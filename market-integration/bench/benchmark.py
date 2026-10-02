"""
Throughput benchmark: one command, one report.

    cd market-integration
    python -m bench.benchmark            # ~15 minutes; writes docs/benchmarks.md
    python -m bench.benchmark --quick    # ~3 minutes, one short window per rate

For each tick rate in --rates it starts the real service (uvicorn, in a
subprocess) with FEED_MODE=load at that rate, connects --clients
WebSocket clients (in separate processes, so the clients aren't the
bottleneck) subscribed to every load instrument, lets it settle, then
measures --windows consecutive windows of --duration seconds, each for:

  * ticks in per second    -- /metrics buffer.received, over the window
  * latency, p50 / p99      -- client receive time minus the tick's
                               timestamp (set when the connector built
                               it): connector -> buffer -> validator ->
                               processor -> gateway -> socket -> client
  * delivery                -- ticks each client got, against the rate
                               (the gateway skips ticks a slow client
                               can't take: conflation)
  * dropped ticks           -- per buffer subscriber, buffer.dropped_counts
  * validator rejections    -- by consumer and rule
  * server CPU and memory   -- psutil, sampled every 0.5 s

A step holds if ingest keeps up (>= 95% of the rate), nothing is
dropped, rejected or skipped by the gateway for a slow client
(conflation), and p99 latency is within --latency-budget. The sweep
stops after two failing steps in a row, then bisects --refine times
between the last step that held and the first that failed. The highest
rate with every step below it holding is the maximum sustained rate;
for the first that fails, the report says which checks failed, in the
order things break.

Latency is wall clock to wall clock on one machine; on Windows both
clocks tick about once a millisecond, so read it to +/- 1 ms.

Then a burst run (--burst-base ticks/s, x --burst-multiplier for
--burst-seconds) reports the same numbers before, during and after the
burst, and the hot-path profile (bench/profile_hot_path.py) is added.
Results go between the markers in docs/benchmarks.md, plus a row in its
history table; the raw numbers go to bench/results/latest.json.

The fixed-income mock keeps running alongside at its normal rate, as
in production, so "ticks in" includes its few ticks a second.
"""

import argparse
import asyncio
import json
import multiprocessing
import os
import platform
import random
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psutil
import websockets

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "benchmarks.md"
RESULTS_JSON = ROOT / "bench" / "results" / "latest.json"
RESULTS_START, RESULTS_END = "<!-- benchmark-results:start -->", "<!-- benchmark-results:end -->"
HISTORY_END = "<!-- benchmark-history:end -->"
MAX_SAMPLES_PER_WORKER = 200_000


# ---------------------------------------------------------------- machine

def _raise_priority(process: psutil.Process) -> None:
    """High priority, so the OS keeps the process on fast cores and
    doesn't throttle it: hybrid laptop CPUs (performance + efficiency
    cores) otherwise give a console-started process the slow ones."""
    try:
        if hasattr(psutil, "HIGH_PRIORITY_CLASS"):
            process.nice(psutil.HIGH_PRIORITY_CLASS)
        else:
            process.nice(-5)
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        pass


def power_state() -> Optional[str]:
    """None when plugged in (or no battery); else a warning."""
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery is None or battery.power_plugged:
        return None
    return (f"ran on battery ({battery.percent:.0f}%): CPUs are throttled, so results are lower "
            "and noisier than plugged in")


# ---------------------------------------------------------------- server

def _get_json(port: int, path: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as response:
        return json.loads(response.read())


class Server:
    """The service in load mode, in its own process and database."""

    def __init__(self, port: int, env: dict, workdir: Path):
        self.port = port
        self.env = {**os.environ, **{k: str(v) for k, v in env.items()},
                    "MARKET_SESSION_OVERRIDE": "open",
                    "MARKET_DB_PATH": str(workdir / f"bench-{port}-{time.time_ns()}.db")}
        self.log = open(workdir / f"server-{port}.log", "ab")
        self.process: Optional[subprocess.Popen] = None

    def __enter__(self) -> "Server":
        self.process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(self.port), "--log-level", "warning"],
            cwd=ROOT, env=self.env, stdout=self.log, stderr=self.log,
        )
        deadline = time.time() + 90
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"server exited during startup; see {self.log.name}")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2)
                return self
            except Exception:
                time.sleep(0.3)
        raise RuntimeError("server did not become healthy within 90 s")

    def __exit__(self, *exc) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.log.close()

    def metrics(self) -> dict:
        return _get_json(self.port, "/metrics?format=json")


# ---------------------------------------------------------------- clients

def _reservoir(samples: list, item, seen: int) -> None:
    if len(samples) < MAX_SAMPLES_PER_WORKER:
        samples.append(item)
    else:
        j = random.randrange(seen)
        if j < MAX_SAMPLES_PER_WORKER:
            samples[j] = item


async def _client(port: int, symbols: list[str], start_at: float, end_at: float, out: dict) -> None:
    uri = f"ws://127.0.0.1:{port}/ws/market"
    async with websockets.connect(uri, max_size=None, ping_interval=None, open_timeout=30) as ws:
        await ws.recv()  # welcome
        await ws.send(json.dumps({"action": "subscribe", "symbols": symbols}))
        received = 0
        while True:
            remaining = end_at - time.time()
            if remaining <= 0:
                break
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            now = time.time()
            if now < start_at or not message.startswith('{"type":"tick"'):
                continue
            # Only the timestamp is needed: find it rather than parse the
            # whole message, so the client stays cheap.
            i = message.find('"timestamp":"') + 13
            sent = datetime.fromisoformat(message[i:message.index('"', i)]).timestamp()
            received += 1
            out["seen"] += 1
            _reservoir(out["samples"], (now, (now - sent) * 1000), out["seen"])
        out["per_client"].append(received)


def client_worker(spec: dict) -> dict:
    """One process's share of the clients (run by multiprocessing)."""
    out = {"samples": [], "seen": 0, "per_client": [], "errors": []}
    _raise_priority(psutil.Process())

    async def run():
        tasks = [
            _client(spec["port"], spec["symbols"], spec["start_at"], spec["end_at"], out)
            for _ in range(spec["clients"])
        ]
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, Exception):
                out["errors"].append(repr(result))

    asyncio.run(run())
    return out


# ---------------------------------------------------------------- measuring

def _percentile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q / 100 * (len(values) - 1))))]


def _delta(before: dict, after: dict) -> dict:
    """What happened between two /metrics snapshots."""
    subs = after["buffer"]["subscribers"]
    validation = {}
    for name, v in after["validation"].items():
        b = before["validation"].get(name, {"passed": 0, "rejected": 0, "by_rule": {}})
        validation[name] = {
            "rejected": v["rejected"] - b["rejected"],
            "by_rule": {r: c - b["by_rule"].get(r, 0) for r, c in v["by_rule"].items() if c - b["by_rule"].get(r, 0)},
        }
    return {
        "received": after["buffer"]["received"] - before["buffer"]["received"],
        "dropped": {n: s["dropped"] - before["buffer"]["subscribers"].get(n, {"dropped": 0})["dropped"]
                    for n, s in subs.items()},
        "max_queue_depth": max((s["depth"] for s in subs.values()), default=0),
        "conflated": after["websocket"]["conflated"] - before["websocket"]["conflated"],
        "validation": validation,
    }


class _Sampler:
    """CPU and memory of the server, every `every` seconds. Covers the
    whole process tree: on Windows a venv's python.exe is a launcher
    that runs the real interpreter as its child."""

    def __init__(self, pid: int, every: float = 0.5):
        self.root = psutil.Process(pid)
        self.every = every
        self.cpu: list[float] = []
        self.rss: list[float] = []
        self._procs: dict[int, psutil.Process] = {}
        self._refresh()

    def _refresh(self) -> list[psutil.Process]:
        for p in [self.root] + self.root.children(recursive=True):
            if p.pid not in self._procs:
                _raise_priority(p)
                p.cpu_percent(None)  # the first reading only sets the baseline
                self._procs[p.pid] = p
        return list(self._procs.values())

    def sample_until(self, end_at: float, on_tick=None) -> None:
        while time.time() < end_at:
            time.sleep(min(self.every, max(0.0, end_at - time.time())))
            cpu = rss = 0.0
            for p in self._refresh():
                try:
                    cpu += p.cpu_percent(None)
                    rss += p.memory_info().rss
                except psutil.NoSuchProcess:
                    self._procs.pop(p.pid, None)
            self.cpu.append(cpu)
            self.rss.append(rss / 2**20)
            if on_tick:
                on_tick()

    def summary(self) -> dict:
        return {
            "cpu_avg": sum(self.cpu) / len(self.cpu) if self.cpu else None,
            "cpu_max": max(self.cpu, default=None),
            "rss_max_mb": max(self.rss, default=None),
        }


def _run_clients(pool, port, clients, workers, symbols, start_at, end_at):
    per = [clients // workers + (1 if i < clients % workers else 0) for i in range(workers)]
    specs = [{"port": port, "clients": n, "symbols": symbols, "start_at": start_at, "end_at": end_at}
             for n in per if n]
    return pool.map_async(client_worker, specs)


def _merge(worker_results: list[dict]) -> dict:
    samples, per_client, errors = [], [], []
    for r in worker_results:
        samples += r["samples"]
        per_client += r["per_client"]
        errors += r["errors"]
    return {"samples": samples, "per_client": per_client, "errors": errors}


def run_step(pool, rate: float, args, workdir: Path) -> dict:
    """One server at `rate`, measured over --windows consecutive windows
    of --duration seconds. The step's figures are the median window's;
    it holds if most windows do. One noisy window (the aggregator's
    once-a-minute database flush, another program) can't decide it,
    but the worst window is reported too."""
    env = {"FEED_MODE": "load", "LOAD_INSTRUMENTS": args.instruments, "LOAD_TICKS_PER_SECOND": rate}
    symbols = [f"LOAD{n:04d}" for n in range(1, args.instruments + 1)]
    with Server(args.port, env, workdir) as server:
        _Sampler(server.process.pid)  # raises its priority before warm-up
        start_at = time.time() + args.warmup
        bounds = [start_at + i * args.duration for i in range(args.windows + 1)]
        pending = _run_clients(pool, server.port, args.clients, args.workers, symbols, start_at, bounds[-1])
        time.sleep(max(0.0, start_at - time.time()))
        sampler = _Sampler(server.process.pid)
        snapshots, cpu_marks = [server.metrics()], [0]
        for end_at in bounds[1:]:
            sampler.sample_until(end_at)
            snapshots.append(server.metrics())
            cpu_marks.append(len(sampler.cpu))
        clients = _merge(pending.get(timeout=args.duration * args.windows + args.warmup + 60))

    windows = []
    for i in range(args.windows):
        lo, hi = bounds[i], bounds[i + 1]
        delta = _delta(snapshots[i], snapshots[i + 1])
        latencies = [lat for t, lat in clients["samples"] if lo <= t < hi]
        cpu = sampler.cpu[cpu_marks[i]:cpu_marks[i + 1]]
        window = {
            "rate": rate,
            "ticks_in_per_second": delta["received"] / args.duration,
            "latency_p50_ms": _percentile(latencies, 50),
            "latency_p99_ms": _percentile(latencies, 99),
            "dropped": delta["dropped"],
            "conflated": delta["conflated"],
            "rejected": {n: v["rejected"] for n, v in delta["validation"].items()},
            "rejected_by_rule": {n: v["by_rule"] for n, v in delta["validation"].items() if v["by_rule"]},
            "max_queue_depth": delta["max_queue_depth"],
            "cpu_avg": sum(cpu) / len(cpu) if cpu else None,
            "client_errors": clients["errors"],
        }
        window["checks"] = _checks(window, args)
        windows.append(window)

    def median(key):
        values = sorted(w[key] for w in windows if w[key] is not None)
        return values[len(values) // 2] if values else None

    def total(key):
        out = {}
        for w in windows:
            for name, n in w[key].items():
                out[name] = out.get(name, 0) + n
        return out

    held = sum(1 for w in windows if not w["checks"])
    expected = rate * args.duration * args.windows
    step = {
        "rate": rate,
        "ticks_in_per_second": median("ticks_in_per_second"),
        "latency_p50_ms": median("latency_p50_ms"),
        "latency_p99_ms": median("latency_p99_ms"),
        "latency_p99_worst_ms": max((w["latency_p99_ms"] for w in windows if w["latency_p99_ms"] is not None),
                                    default=None),
        "delivered": (sum(clients["per_client"]) / len(clients["per_client"]) / expected)
                     if clients["per_client"] else 0.0,
        "dropped": total("dropped"),
        "conflated": sum(w["conflated"] for w in windows),
        "rejected": total("rejected"),
        "rejected_by_rule": {},
        "max_queue_depth": max(w["max_queue_depth"] for w in windows),
        **sampler.summary(),
        "windows": len(windows),
        "windows_held": held,
        "window_details": [{k: w[k] for k in ("ticks_in_per_second", "latency_p50_ms", "latency_p99_ms",
                                              "cpu_avg", "checks")} for w in windows],
        "client_errors": clients["errors"][:5],
    }
    for w in windows:
        for consumer, by_rule in w["rejected_by_rule"].items():
            into = step["rejected_by_rule"].setdefault(consumer, {})
            for rule, n in by_rule.items():
                into[rule] = into.get(rule, 0) + n
    # A check fails the step if it failed in most windows.
    failing = [c for c in CHECKS if sum(1 for w in windows if c in w["checks"]) * 2 > len(windows)]
    step["failures"] = [_describe(c, step, args) for c in failing]
    return step


def _max_sustained(steps: list[dict]) -> Optional[float]:
    """The highest rate where it, and every lower rate tried, held."""
    best = None
    for s in sorted(steps, key=lambda s: s["rate"]):
        if s["failures"]:
            break
        best = s["rate"]
    return best


# The checks a window must pass, in the order things break.
CHECKS = ("ingest", "dropped", "rejected", "latency", "conflated", "clients")


def _checks(window: dict, args) -> list[str]:
    """Which checks a window failed."""
    failed = []
    if window["ticks_in_per_second"] < 0.95 * window["rate"]:
        failed.append("ingest")
    if any(window["dropped"].values()):
        failed.append("dropped")
    if any(window["rejected"].values()):
        failed.append("rejected")
    if window["latency_p99_ms"] is None or window["latency_p99_ms"] > args.latency_budget:
        failed.append("latency")
    if window["conflated"]:
        failed.append("conflated")
    if window["client_errors"]:
        failed.append("clients")
    return failed


def _describe(check: str, step: dict, args) -> str:
    if check == "ingest":
        return "ingest fell behind the rate"
    if check == "dropped":
        return "buffer dropped ticks (" + ", ".join(f"{n}: {c:,}" for n, c in step["dropped"].items() if c) + ")"
    if check == "rejected":
        return "validator rejected ticks"
    if check == "latency":
        return f"p99 latency over {args.latency_budget:g} ms"
    if check == "conflated":
        return f"gateway skipped {step['conflated']:,} ticks for slow clients (conflation)"
    return "client connections failed"


def run_burst(pool, args, workdir: Path) -> dict:
    """Base rate, then x multiplier for burst_seconds, then base again;
    each phase found from the per-second ingest series."""
    lead, tail = 20.0, 15.0
    env = {"FEED_MODE": "load", "LOAD_INSTRUMENTS": args.instruments, "LOAD_TICKS_PER_SECOND": args.burst_base,
           "LOAD_BURST_MULTIPLIER": args.burst_multiplier, "LOAD_BURST_SECONDS": args.burst_seconds,
           "LOAD_BURST_AFTER_SECONDS": lead}
    symbols = [f"LOAD{n:04d}" for n in range(1, args.instruments + 1)]
    with Server(args.port, env, workdir) as server:
        # The stream starts during startup, before /health says healthy,
        # so the burst is over by healthy + lead + burst_seconds.
        healthy_at = time.time()
        _Sampler(server.process.pid)  # raises its priority before warm-up
        start_at = healthy_at + args.warmup
        end_at = healthy_at + lead + args.burst_seconds + tail
        pending = _run_clients(pool, server.port, args.clients, args.workers, symbols, start_at, end_at)
        time.sleep(max(0.0, start_at - time.time()))
        series = []  # (time, metrics)

        def poll():
            series.append((time.time(), server.metrics()))

        poll()
        sampler = _Sampler(server.process.pid, every=1.0)
        sampler.sample_until(end_at, on_tick=poll)
        clients = _merge(pending.get(timeout=end_at - time.time() + 60))

    # Per-second ingest; the burst is where it runs well above base.
    rates = [((t0 + t1) / 2, (m1["buffer"]["received"] - m0["buffer"]["received"]) / (t1 - t0))
             for (t0, m0), (t1, m1) in zip(series, series[1:])]
    threshold = args.burst_base * (1 + (args.burst_multiplier - 1) / 2)
    hot = [t for t, r in rates if r >= threshold]
    burst_from, burst_to = (min(hot), max(hot)) if hot else (end_at, end_at)

    def phase(name, lo, hi):
        lat = [lat for t, lat in clients["samples"] if lo <= t < hi]
        window = [(t, m) for t, m in series if lo <= t <= hi]
        d = _delta(window[0][1], window[-1][1]) if len(window) >= 2 else None
        seconds = window[-1][0] - window[0][0] if len(window) >= 2 else 0
        return {
            "phase": name,
            "seconds": seconds,
            "ticks_in_per_second": d["received"] / seconds if d and seconds else None,
            "latency_p50_ms": _percentile(lat, 50),
            "latency_p99_ms": _percentile(lat, 99),
            "dropped": sum(d["dropped"].values()) if d else None,
            "conflated": d["conflated"] if d else None,
            "rejected": sum(v["rejected"] for v in d["validation"].values()) if d else None,
        }

    return {
        "base": args.burst_base,
        "multiplier": args.burst_multiplier,
        "burst_seconds": args.burst_seconds,
        "detected": bool(hot),
        "phases": [
            phase("before", start_at, burst_from - 1),
            phase("during", burst_from, burst_to),
            phase("after", burst_to + 3, end_at),
        ],
        "peak_ticks_in_per_second": max((r for _, r in rates), default=None),
        **sampler.summary(),
        "client_errors": clients["errors"][:5],
    }


# ---------------------------------------------------------------- report

def _fmt(value, spec=",.0f", none="—") -> str:
    return none if value is None else format(value, spec)


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def render(results: dict) -> str:
    meta = results["meta"]
    lines = [
        f"Run {meta['date']} on commit `{meta['commit']}`: {meta['machine']}. "
        f"{meta['instruments']} instruments, {meta['clients']} WebSocket clients each subscribed to all of them, "
        f"{meta['windows']} windows of {meta['duration']:g} s per rate after {meta['warmup']:g} s warm-up; "
        f"p99 budget {meta['latency_budget']:g} ms. Figures are the median window's; a rate holds if most "
        "windows pass every check. Delivered is ticks each client got against the target rate.",
        "",
    ]
    if meta.get("power_warning"):
        lines += [f"> **Warning:** {meta['power_warning']}.", ""]
    best = results["max_sustained"]
    first_fail = next((s for s in results["steps"] if s["failures"]), None)
    lines.append(f"**Maximum sustained rate: {_fmt(best)} ticks/s**" if best else
                 "**No step held: even the lowest rate failed a check.**")
    if first_fail:
        lines.append(f"(first failure at {_fmt(first_fail['rate'])} ticks/s: {first_fail['failures'][0]}).")
    lines += [
        "",
        "#### Rate sweep",
        "",
        "| Target ticks/s | Ticks in/s | p50 ms | p99 ms | p99 worst window | Delivered | Dropped | Conflated "
        "| Rejected | CPU avg / max | Memory | Windows held | Holds? |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---|",
    ]
    for s in results["steps"]:
        lines.append(
            f"| {_fmt(s['rate'])} | {_fmt(s['ticks_in_per_second'])} | {_fmt(s['latency_p50_ms'], '.1f')} "
            f"| {_fmt(s['latency_p99_ms'], '.1f')} | {_fmt(s['latency_p99_worst_ms'], '.1f')} "
            f"| {s['delivered']:.0%} | {_fmt(sum(s['dropped'].values()))} "
            f"| {_fmt(s['conflated'])} | {_fmt(sum(s['rejected'].values()))} "
            f"| {_fmt(s['cpu_avg'], '.0f')}% / {_fmt(s['cpu_max'], '.0f')}% | {_fmt(s['rss_max_mb'], '.0f')} MB "
            f"| {s['windows_held']}/{s['windows']} "
            f"| {'yes' if not s['failures'] else 'no: ' + '; '.join(s['failures'])} |"
        )
    rules = {}
    for s in results["steps"]:
        for consumer, by_rule in s["rejected_by_rule"].items():
            for rule, n in by_rule.items():
                rules[(consumer, rule)] = rules.get((consumer, rule), 0) + n
    if rules:
        lines += ["", "Rejections by rule across the sweep: " +
                  ", ".join(f"{c}/{r}: {n:,}" for (c, r), n in sorted(rules.items())) + "."]
    lines += ["", "CPU is % of one core (the service is one asyncio process).", ""]

    burst = results.get("burst")
    if burst:
        lines += [
            f"#### Burst: {_fmt(burst['base'])} ticks/s, x{burst['multiplier']:g} for {burst['burst_seconds']:g} s",
            "",
        ]
        if not burst["detected"]:
            lines += ["The burst was not detected in the ingest series (see raw JSON).", ""]
        lines += [
            "| Phase | Seconds | Ticks in/s | p50 ms | p99 ms | Dropped | Conflated | Rejected |",
            "|:---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for p in burst["phases"]:
            lines.append(
                f"| {p['phase']} | {_fmt(p['seconds'], '.0f')} | {_fmt(p['ticks_in_per_second'])} "
                f"| {_fmt(p['latency_p50_ms'], '.1f')} | {_fmt(p['latency_p99_ms'], '.1f')} "
                f"| {_fmt(p['dropped'])} | {_fmt(p['conflated'])} | {_fmt(p['rejected'])} |"
            )
        lines += ["", f"Peak ingest {_fmt(burst['peak_ticks_in_per_second'])} ticks/s; "
                      f"CPU max {_fmt(burst['cpu_max'], '.0f')}%, memory max {_fmt(burst['rss_max_mb'], '.0f')} MB.", ""]

    profile = results.get("profile")
    if profile:
        total = sum(r["us_per_tick"] for r in profile["stages"])
        lines += [
            f"#### Hot path, per tick ({meta['clients']} clients)",
            "",
            "| Stage | µs per call | Calls per tick | µs per tick | Share |",
            "|:---|---:|---:|---:|---:|",
        ]
        for r in profile["stages"]:
            lines.append(f"| {r['stage']} | {r['us_per_call']:.2f} | {r['calls_per_tick']} "
                         f"| {r['us_per_tick']:.2f} | {r['us_per_tick'] / total:.0%} |")
        pipe = profile["pipeline"]
        lines += [
            f"| **Total** | | | **{total:.1f}** | |",
            "",
            f"That is a ceiling of about {_fmt(1e6 / total)} ticks/s on one core before any network I/O. "
            f"The in-process pipeline (no sockets) ran {_fmt(pipe['unprofiled']['ticks_per_second'])} ticks/s; "
            f"its clients were sent {_fmt(pipe['unprofiled']['messages_sent'])} of "
            f"{_fmt(pipe['ticks'] * pipe['clients'])} possible messages (the rest conflated).",
            "",
            "Top functions by own time under cProfile:",
            "",
            "| Share | Calls | Function |",
            "|---:|---:|:---|",
        ]
        for r in pipe["profile"]:
            lines.append(f"| {r['own_share']:.1%} | {r['calls']:,} | `{r['function']}` |")
        lines.append("")
    return "\n".join(lines)


def write_report(results: dict) -> None:
    RESULTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_JSON.write_text(json.dumps(results, indent=2), encoding="utf-8")
    doc = DOC.read_text(encoding="utf-8")
    start, end = doc.index(RESULTS_START) + len(RESULTS_START), doc.index(RESULTS_END)
    doc = doc[:start] + "\n" + render(results) + "\n" + doc[end:]

    meta, at_target = results["meta"], None
    target = next((s for s in results["steps"] if s["rate"] == meta.get("target_rate")), None)
    if target:
        at_target = f"{_fmt(target['latency_p99_ms'], '.1f')} ms"
    burst = results.get("burst")
    burst_p99 = _fmt(burst["phases"][1]["latency_p99_ms"], ".1f") + " ms" if burst else "—"
    row = (f"| {meta['date'][:10]} | `{meta['commit']}` | {_fmt(results['max_sustained'])} "
           f"| {at_target or '—'} | {burst_p99} | {meta['clients']} | {meta['machine']} |\n")
    history_at = doc.index(HISTORY_END)
    doc = doc[:history_at] + row + doc[history_at:]
    DOC.write_text(doc, encoding="utf-8")


# ---------------------------------------------------------------- main

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rates", default="250,500,1000,2000,4000,8000",
                        help="target ticks/s to step through (default: %(default)s)")
    parser.add_argument("--target-rate", type=float, default=1000,
                        help="the provisional target, highlighted in the history (default: %(default)s)")
    parser.add_argument("--instruments", type=int, default=100)
    parser.add_argument("--clients", type=int, default=10)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1),
                        help="processes the clients are spread over")
    parser.add_argument("--duration", type=float, default=20, help="seconds per measurement window")
    parser.add_argument("--windows", type=int, default=3,
                        help="windows per rate; 3 x 20 s spans one aggregator flush (every 60 s)")
    parser.add_argument("--warmup", type=float, default=5)
    parser.add_argument("--latency-budget", type=float, default=100, help="p99 ms (default: %(default)s)")
    parser.add_argument("--refine", type=int, default=3,
                        help="bisection steps between the last rate that held and the first that failed")
    parser.add_argument("--burst-base", type=float, default=100)
    parser.add_argument("--burst-multiplier", type=float, default=10)
    parser.add_argument("--burst-seconds", type=float, default=30)
    parser.add_argument("--no-burst", action="store_true")
    parser.add_argument("--no-profile", action="store_true")
    parser.add_argument("--no-write", action="store_true", help="print the report instead of updating the docs")
    parser.add_argument("--quick", action="store_true", help="shorter steps and burst, for a fast check")
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()
    if args.quick:
        args.duration, args.windows, args.warmup, args.burst_seconds = 8, 1, 4, 10

    rates = [float(r) for r in args.rates.split(",")]
    workdir = Path(tempfile.mkdtemp(prefix="market-bench-"))
    cpu = platform.processor() or platform.machine()
    results = {
        "meta": {
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "commit": _git_commit(),
            "machine": f"{platform.system()} {platform.release()}, {cpu}, {os.cpu_count()} logical CPUs, "
                       f"Python {platform.python_version()}",
            "instruments": args.instruments, "clients": args.clients, "duration": args.duration,
            "windows": args.windows,
            "warmup": args.warmup, "latency_budget": args.latency_budget, "target_rate": args.target_rate,
            "power_warning": power_state(),
        },
        "steps": [],
    }

    if results["meta"]["power_warning"]:
        print(f"WARNING: {results['meta']['power_warning']}. Plug in for numbers worth keeping.", flush=True)

    with multiprocessing.Pool(args.workers) as pool:
        failing_in_a_row = 0
        for rate in rates:
            print(f"step {rate:,.0f} ticks/s ...", flush=True)
            step = run_step(pool, rate, args, workdir)
            results["steps"].append(step)
            print(f"  in {step['ticks_in_per_second']:,.0f}/s, p99 {_fmt(step['latency_p99_ms'], '.1f')} ms, "
                  f"delivered {step['delivered']:.0%}, cpu {_fmt(step['cpu_avg'], '.0f')}% "
                  f"-> {'holds' if not step['failures'] else '; '.join(step['failures'])}", flush=True)
            failing_in_a_row = failing_in_a_row + 1 if step["failures"] else 0
            if failing_in_a_row == 2:
                break
        best = _max_sustained(results["steps"])
        failed = next((s["rate"] for s in sorted(results["steps"], key=lambda s: s["rate"]) if s["failures"]), None)
        if best is not None and failed is not None:
            lo, hi = best, failed
            for _ in range(args.refine):
                mid = round((lo + hi) / 2 / 50) * 50
                if mid in (lo, hi):
                    break
                print(f"refine {mid:,.0f} ticks/s ...", flush=True)
                step = run_step(pool, mid, args, workdir)
                results["steps"].append(step)
                print(f"  in {step['ticks_in_per_second']:,.0f}/s, p99 {_fmt(step['latency_p99_ms'], '.1f')} ms "
                      f"-> {'holds' if not step['failures'] else '; '.join(step['failures'])}", flush=True)
                lo, hi = (mid, hi) if not step["failures"] else (lo, mid)
        results["steps"].sort(key=lambda s: s["rate"])
        results["max_sustained"] = _max_sustained(results["steps"])

        if not args.no_burst:
            print(f"burst {args.burst_base:,.0f} ticks/s x{args.burst_multiplier:g} "
                  f"for {args.burst_seconds:g} s ...", flush=True)
            results["burst"] = run_burst(pool, args, workdir)

    if not args.no_profile:
        print("profiling the hot path ...", flush=True)
        from bench import profile_hot_path

        results["profile"] = {
            "stages": asyncio.run(profile_hot_path.stages(args.clients)),
            "pipeline": profile_hot_path.pipeline(clients=args.clients),
        }

    if args.no_write:
        print(render(results))
    else:
        write_report(results)
        print(f"\nwrote {DOC.relative_to(ROOT)} and {RESULTS_JSON.relative_to(ROOT)}")
    print(f"max sustained: {_fmt(results['max_sustained'])} ticks/s")


if __name__ == "__main__":
    main()
