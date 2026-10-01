"""
/metrics in the Prometheus text exposition format: the buffer's per
subscriber queue depth and drops, and the aggregator's incomplete
candles. Hand-written rather than via prometheus_client -- a handful of
values doesn't need the dependency.

A drop on a lossless subscriber (aggregator, alerts) means data loss;
alert on

    increase(market_buffer_dropped_ticks_total{policy="lossless"}[5m]) > 0

Drops on the drop_oldest display branch are expected under load.
"""

from app.aggregation.market_aggregator import MarketAggregator
from app.queue.market_buffer import MarketDataBuffer


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**labels: str) -> str:
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"


def render(buffer: MarketDataBuffer, aggregator: MarketAggregator) -> str:
    stats = buffer.stats()
    lines: list[str] = []

    def metric(name: str, kind: str, help_: str, samples: list[tuple[str, float]]) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(f"{name}{labels} {value}" for labels, value in samples)

    metric(
        "market_buffer_ticks_total", "counter",
        "Ticks the buffer has read from the feed.",
        [("", buffer.ticks_total)],
    )
    per_sub = [(s, _labels(subscriber=s["subscriber"], policy=s["policy"])) for s in stats]
    metric(
        "market_buffer_dropped_ticks_total", "counter",
        "Ticks a subscriber lost because its queue was full.",
        [(labels, s["dropped"]) for s, labels in per_sub],
    )
    metric(
        "market_buffer_queue_depth", "gauge",
        "Ticks waiting in a subscriber's queue.",
        [(labels, s["depth"]) for s, labels in per_sub],
    )
    metric(
        "market_buffer_queue_peak_depth", "gauge",
        "Most ticks ever waiting in a subscriber's queue at once since startup.",
        [(labels, s["peak_depth"]) for s, labels in per_sub],
    )
    metric(
        "market_buffer_queue_capacity", "gauge",
        "A subscriber's queue size.",
        [(labels, s["capacity"]) for s, labels in per_sub],
    )
    metric(
        "market_buffer_blocked_seconds_total", "counter",
        "Seconds the feed was paused waiting for a full lossless subscriber.",
        [(labels, round(s["blocked_seconds"], 6)) for s, labels in per_sub],
    )
    metric(
        "market_aggregator_incomplete_candles_total", "counter",
        "Live candles flagged possibly_incomplete because a tick was dropped.",
        [("", aggregator.incomplete_candles)],
    )
    return "\n".join(lines) + "\n"
