"""
Where a tick's time goes: the per-tick hot path, measured in-process
with no network, so the numbers are the pipeline's own.

    python -m bench.profile_hot_path [--clients 10] [--ticks 20000]

Two views:

  * stages(): each step a tick goes through, timed on its own, in
    microseconds per call -- building the MarketData (Pydantic
    validation), the business-rule validator, the processor, the
    aggregator, model_dump to JSON-ready dicts, json.dumps of the
    WebSocket message (done once per client: send_json serializes per
    connection) and the gateway's broadcast fan-out;
  * pipeline(): the real buffer -> validator -> processor -> gateway
    chain plus the aggregator, driven as fast as it will go with
    `clients` in-memory WebSocket clients, under cProfile. Gives the
    in-process ceiling in ticks/s and the functions that cost most.

bench/benchmark.py runs both and puts them in docs/benchmarks.md.
"""

import argparse
import asyncio
import cProfile
import json
import pstats
import tempfile
import time
from pathlib import Path

from app.aggregation.market_aggregator import MarketAggregator
from app.connectors.load_connector import LoadConnector
from app.gateways.websocket_gateway import WebSocketGateway
from app.models.market_data import MarketData
from app.processors.market_processor import MarketProcessor
from app.queue.market_buffer import MarketDataBuffer
from app.validation.market_validator import ValidatingStream, validate_tick


def _send_json_cost(message: dict) -> str:
    # What Starlette's WebSocket.send_json does before writing the frame.
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False)


class _FakeWebSocket:
    """Accepts and serializes like a real connection; sends nowhere."""

    def __init__(self):
        self.sent = 0

    async def accept(self):
        pass

    async def send_json(self, data):
        _send_json_cost(data)
        self.sent += 1
        # A real send awaits the socket, letting other tasks run.
        await asyncio.sleep(0)


def _timeit(fn, n: int) -> float:
    """Microseconds per call, best of three runs of n calls."""
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        for _ in range(n):
            fn()
        best = min(best, time.perf_counter() - start)
    return best / n * 1e6


async def _connected_gateway(symbols: list[str], clients: int):
    gateway = WebSocketGateway(symbols=symbols, snapshot=lambda s: {})
    sockets = []
    for _ in range(clients):
        ws = _FakeWebSocket()
        client = await gateway.connect(ws)
        gateway.handle_message(client, json.dumps({"action": "subscribe", "symbols": symbols}))
        sockets.append(ws)
    return gateway, sockets


async def stages(clients: int = 10, n: int = 5000) -> list[dict]:
    """Per-call cost of each hot-path step, in microseconds."""
    load = LoadConnector(instruments=100, ticks_per_second=1000)
    await load.connect()
    symbol = load.symbols[0]
    tick = load._tick(symbol, trade=True)
    fields = tick.model_dump()
    message = {"type": "tick", "data": tick.model_dump(mode="json")}
    processor = MarketProcessor()
    with tempfile.TemporaryDirectory() as tmp:
        aggregator = MarketAggregator(_never(), db_path=Path(tmp) / "bench.db", flush_interval_seconds=3600)
        gateway, sockets = await _connected_gateway(load.symbols, clients)

        # (label, call, calls per tick, async?). validate_tick runs twice
        # per tick: once for real-time delivery, once in the aggregator.
        # broadcast() includes model_dump; json.dumps happens later, once
        # per client, in each client's send loop.
        rows = [
            ("Build MarketData (Pydantic validation)", lambda: MarketData(**fields), 1, False),
            ("Business-rule validation (validate_tick)", lambda: validate_tick(tick), 2, False),
            ("Processor: update latest quote", lambda: processor.process(tick), 1, True),
            ("Aggregator: fold into candles", lambda: aggregator._record(tick), 1, False),
            (f"Gateway broadcast: model_dump + fan-out to {clients} clients",
             lambda: gateway.broadcast(tick), 1, True),
            ("json.dumps of the message (per client)", lambda: _send_json_cost(message), clients, False),
        ]
        results = []
        for label, fn, per_tick, is_async in rows:
            us = await _time_async(fn, n) if is_async else _timeit(fn, n)
            results.append({"stage": label, "us_per_call": us, "calls_per_tick": per_tick,
                            "us_per_tick": us * per_tick})
        for client in list(gateway._clients):
            await gateway.disconnect(client)
    return results


async def _time_async(make, n: int) -> float:
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        for _ in range(n):
            await make()
        best = min(best, time.perf_counter() - start)
    return best / n * 1e6


async def _never():
    await asyncio.Event().wait()
    yield  # pragma: no cover


async def _pipeline(ticks: int, clients: int) -> dict:
    load = LoadConnector(instruments=100, ticks_per_second=1000)
    await load.connect()

    async def source():
        for i in range(ticks):
            yield load._tick(load.symbols[i % len(load.symbols)], trade=True)
            # As CompositeConnector does: let consumers run between ticks.
            await asyncio.sleep(0)

    with tempfile.TemporaryDirectory() as tmp:
        buffer = MarketDataBuffer(source(), maxsize=ticks + 10)  # big enough never to drop
        validated = ValidatingStream(buffer.subscribe("processor"), name="processor")
        aggregator = MarketAggregator(buffer.subscribe("aggregator"), db_path=Path(tmp) / "bench.db",
                                      flush_interval_seconds=3600)
        processor = MarketProcessor()
        gateway, sockets = await _connected_gateway(load.symbols, clients)
        done = asyncio.Event()
        processed = 0

        async def deliver(tick):
            nonlocal processed
            await gateway.broadcast(tick)
            processed += 1
            if processed == ticks:
                done.set()

        start = time.perf_counter()
        await buffer.start()
        await aggregator.start()
        consumer = asyncio.create_task(processor.consume(validated, on_processed=deliver))
        await done.wait()
        # Let the client send loops drain what they hold.
        for _ in range(100):
            await asyncio.sleep(0)
        elapsed = time.perf_counter() - start
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await aggregator.stop(final_flush=False)
        await buffer.stop()
        for client in list(gateway._clients):
            await gateway.disconnect(client)
    return {
        "ticks": ticks,
        "clients": clients,
        "seconds": elapsed,
        "ticks_per_second": ticks / elapsed,
        "messages_sent": sum(s.sent for s in sockets),
        "rejected": validated.rejections.rejected,
    }


def pipeline(ticks: int = 20000, clients: int = 10, top: int = 15) -> dict:
    """Run the in-process pipeline under cProfile; the throughput and
    the `top` functions by own time."""
    profiler = cProfile.Profile()
    profiler.enable()
    result = asyncio.run(_pipeline(ticks, clients))
    profiler.disable()
    stats = pstats.Stats(profiler)
    total = sum(row[2] for row in stats.stats.values())  # total own time
    rows = sorted(stats.stats.items(), key=lambda kv: kv[1][2], reverse=True)[:top]
    result["profile"] = [
        {
            "function": f"{Path(file).name}:{line} {name}" if file != "~" else name,
            "calls": nc,
            "own_seconds": tt,
            "own_share": tt / total if total else 0.0,
            "cumulative_seconds": ct,
        }
        for (file, line, name), (cc, nc, tt, ct, callers) in rows
    ]
    # Without the profiler's overhead, for the headline number.
    result["unprofiled"] = asyncio.run(_pipeline(ticks, clients))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clients", type=int, default=10)
    parser.add_argument("--ticks", type=int, default=20000)
    args = parser.parse_args()

    for row in asyncio.run(stages(args.clients)):
        print(f"{row['stage']:<48} {row['us_per_call']:8.2f} us x{row['calls_per_tick']:<3} = {row['us_per_tick']:8.2f} us/tick")
    result = pipeline(args.ticks, args.clients)
    print(f"\nIn-process pipeline: {result['unprofiled']['ticks_per_second']:,.0f} ticks/s "
          f"with {args.clients} clients ({result['ticks_per_second']:,.0f} under cProfile)")
    for row in result["profile"]:
        print(f"  {row['own_share']:6.1%}  {row['calls']:>9,}  {row['function']}")


if __name__ == "__main__":
    main()
