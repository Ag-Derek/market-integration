# Throughput benchmarks

How many ticks per second the service handles before real data
arrives, and what gives out first. Rerun after any change to the tick
path (connector, buffer, validator, processor, gateway, aggregator) and
keep the history table below.

## Run it

```bash
cd market-integration
python -m bench.benchmark            # ~15 minutes: sweep, burst, profile; updates this file
python -m bench.benchmark --quick    # ~3 minutes, shorter steps
python -m bench.benchmark --help     # rates, clients, instruments, budget, burst shape
python -m bench.profile_hot_path     # just the per-tick profile, in-process
```

The results section below and `bench/results/latest.json` are
regenerated on each run, and a row is added to the history table.
Needs `psutil` (in `requirements-dev.txt`). Close other heavy programs
first: CPU contention shows up as latency.

## Provisional targets

There is no GSE or GFIM feed specification yet, so the expected peak is
an estimate: about 200 instruments (42 equities, ~160 bills and bonds)
each updating at most every couple of seconds at the busiest moment
(the open, a rate announcement) gives **~100 ticks/s**. Targets are set
at 10x that. Replace both when the GSE docs give real message rates.

| # | Target | Pass if |
|:--|:--|:--|
| T1 | Sustained 1,000 ticks/s (10x expected peak), 10 clients subscribed to everything | no buffer drops, no validator rejections, no gateway conflation, p99 latency ≤ 100 ms |
| T2 | Burst: 100 ticks/s, then 10x (1,000 ticks/s) for 30 s, then back | same as T1, during the burst and after it |
| T3 | Headroom at the T1 rate | server CPU ≤ 70% of one core, so requests and page loads stay responsive |

## Method

`bench/benchmark.py` starts the real service with `FEED_MODE=load`: the
GSE equities mock is swapped for `LoadConnector`
(`app/connectors/load_connector.py`), which emits synthetic equities at
an exact rate, overall or per instrument, with optional bursts
(`LOAD_*` settings in `app/config.py`). The fixed-income mock keeps
running alongside, as in production. Each tick is a full `MarketData`
built through Pydantic, as a real connector's `normalize()` would, and
goes through the whole pipeline: buffer → validator → processor →
gateway → WebSocket, plus the aggregator branch to SQLite.

Clients are real WebSocket connections from separate processes, each
subscribed to every load instrument (the worst case for fan-out).
Latency is the client's receive time minus the tick's `timestamp`, set
when the connector built it. On Windows both clocks tick about once a
millisecond, so read latency to ±1 ms.

For each rate the benchmark measures, over a fixed window after
warm-up: ticks in per second (`/metrics`), p50/p99 latency, ticks each
client received, buffer drops per subscriber, validator rejections (by
rule), gateway conflation (ticks skipped for a slow client), and server
CPU and memory. A step holds only if all the T1 checks pass. The sweep
stops after two failing steps, then bisects between the last step that
held and the first that failed.

`bench/profile_hot_path.py` times each per-tick stage in-process and
runs the pipeline under cProfile with in-memory clients.

## Results

<!-- benchmark-results:start -->
Not run yet: run `python -m bench.benchmark`, plugged in, to fill this in.
<!-- benchmark-results:end -->

## Findings

Written by hand after each significant run; the numbers above are the
latest.

### 2026-10-02, preliminary (laptop on battery, 10 clients)

From a run cut short during bisection, and the hot-path profile. Rerun
plugged in before relying on the exact figures.

- **T1 (1,000 ticks/s) holds, without much room.** 250, 500 and
  1,000 ticks/s passed every check: p99 18, 26 and 60 ms, no drops,
  rejections or conflation. At 1,000 the server used ~72% of a core,
  so **T3 (≤ 70% CPU) is not met**.
- **It breaks at roughly 1,000-1,250 ticks/s, and CPU goes first.** At
  1,500 and above the single asyncio process sits at ~97% of a core.
  The connector can't hand ticks over fast enough (ingest 800-1,200/s
  against the rate), so the backlog builds *before* the buffer:
  latency climbs past a second while the buffer reports no drops. In
  other words, `dropped_ticks` stays at 0 while the service falls
  behind; watch latency and ingest rate, not just drops.
- **Where the time goes: re-serializing for every client.** Of ~97 µs
  per tick with 10 clients, ~78 µs (81%) is `json.dumps` of the same
  message once per client (`send_json` in each client's send loop).
  Pydantic validation (~4 µs), the business-rule validator (~2 µs for
  both consumers), the aggregator (~6 µs) and the broadcast fan-out
  (~6 µs) are small by comparison. Serializing each tick once and
  sending the same text to every client should raise the ceiling well
  past 2,000 ticks/s and make it nearly independent of client count.
  This is the first optimisation to make; rerun the benchmark after it.
- **Measurement notes.** This laptop has a hybrid CPU (performance and
  efficiency cores); on battery, or at normal priority, Windows runs the
  service on slow cores and the same rate costs twice the CPU. The
  benchmark now raises priority and flags battery runs. Stop any dev
  server (`uvicorn --reload`) before running: it competes for the CPU.

## History

| Date | Commit | Max sustained ticks/s | p99 at 1,000 ticks/s | p99 during burst | Clients | Machine |
|:--|:--|--:|--:|--:|--:|:--|
<!-- benchmark-history:end -->
