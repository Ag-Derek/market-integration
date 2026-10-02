"""
Pipeline metrics behind GET /metrics: how much the buffer is dropping
and holding, how many WebSocket clients are connected, what validation
turns away (by rule), the aggregator's last database write, and each
feed's reconnects.

collect() reads the counters the components already keep into one
snapshot (served as JSON); to_prometheus() renders that snapshot in the
Prometheus text exposition format, so a Prometheus server can scrape
/metrics directly with no client library.
"""

from datetime import datetime
from typing import Iterable, Mapping, Optional

from app.aggregation.market_aggregator import MarketAggregator
from app.connectors.supervisor import CONNECTED, SupervisedConnector
from app.gateways.websocket_gateway import WebSocketGateway
from app.queue.market_buffer import MarketDataBuffer
from app.validation.market_validator import RejectionCounter


def collect(
    *,
    buffer: MarketDataBuffer,
    gateway: WebSocketGateway,
    validators: Mapping[str, RejectionCounter],
    aggregator: MarketAggregator,
    feeds: Iterable[SupervisedConnector],
) -> dict:
    depths = buffer.queue_depths
    dropped = buffer.dropped_counts
    return {
        "buffer": {
            "received": buffer.received,
            "capacity": buffer.maxsize,
            "subscribers": {
                name: {"mode": mode, "depth": depths.get(name, 0), "dropped": dropped.get(name, 0)}
                for name, mode in buffer.subscriber_modes.items()
            },
        },
        "websocket": {
            "clients": gateway.client_count,
            "subscriptions": gateway.subscription_count,
            "conflated": gateway.conflated_count,
        },
        "validation": {name: counter.snapshot() for name, counter in validators.items()},
        "aggregator": {
            "flushes": aggregator.flushes,
            "last_flush_at": aggregator.last_flush_at.isoformat() if aggregator.last_flush_at else None,
            "last_flush_seconds": aggregator.last_flush_seconds,
            "last_flush_rows": aggregator.last_flush_rows,
        },
        "feeds": {
            feed.name: {
                "state": feed.state,
                "up": feed.running and feed.state == CONNECTED,
                "reconnects": feed.reconnects,
                "attempt": feed.attempt,
            }
            for feed in feeds
        },
    }


def summary(snapshot: dict) -> dict:
    """The few numbers /health shows."""
    subscribers = snapshot["buffer"]["subscribers"].values()
    return {
        "dropped_ticks": sum(s["dropped"] for s in subscribers),
        "max_queue_depth": max((s["depth"] for s in subscribers), default=0),
        "queue_capacity": snapshot["buffer"]["capacity"],
        "websocket_clients": snapshot["websocket"]["clients"],
        "rejected_ticks": {name: v["rejected"] for name, v in snapshot["validation"].items()},
        "last_flush_at": snapshot["aggregator"]["last_flush_at"],
        "last_flush_seconds": snapshot["aggregator"]["last_flush_seconds"],
    }


# ---------------------------------------------------------------- Prometheus

def _escape(value) -> str:
    """A label value as the exposition format wants it: backslash, double
    quote and newline escaped."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(labels: Optional[dict]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"


def _number(value: float) -> str:
    """Whole numbers as integers, others at full precision (never :g,
    which keeps 6 digits and would round a Unix timestamp by minutes)."""
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


class _Exposition:
    def __init__(self):
        self.lines: list[str] = []

    def metric(self, name: str, kind: str, help_: str, samples: Iterable[tuple[Optional[dict], float]]) -> None:
        self.lines.append(f"# HELP {name} {help_}")
        self.lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            self.lines.append(f"{name}{_labels(labels)} {_number(value)}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def to_prometheus(snapshot: dict) -> str:
    out = _Exposition()
    buffer = snapshot["buffer"]
    subscribers = buffer["subscribers"]
    out.metric("market_buffer_received_ticks_total", "counter",
               "Ticks taken off the connector by the buffer.", [(None, buffer["received"])])
    out.metric("market_buffer_dropped_ticks_total", "counter",
               "Ticks dropped (oldest first) because a subscriber fell behind.",
               [({"subscriber": n}, s["dropped"]) for n, s in subscribers.items()])
    out.metric("market_buffer_queue_depth", "gauge",
               "Ticks waiting to be read, per subscriber (symbols with an unread tick, for conflated ones).",
               [({"subscriber": n, "mode": s["mode"]}, s["depth"]) for n, s in subscribers.items()])
    out.metric("market_buffer_queue_capacity", "gauge",
               "Per-subscriber queue size before ticks are dropped.", [(None, buffer["capacity"])])

    ws = snapshot["websocket"]
    out.metric("market_websocket_clients", "gauge", "Connected WebSocket clients.", [(None, ws["clients"])])
    out.metric("market_websocket_subscriptions", "gauge",
               "Symbol subscriptions across all WebSocket clients.", [(None, ws["subscriptions"])])
    out.metric("market_websocket_conflated_ticks_total", "counter",
               "Ticks skipped for a slow client because a newer one for the symbol replaced them.",
               [(None, ws["conflated"])])

    validation = snapshot["validation"]
    out.metric("market_validation_ticks_total", "counter",
               "Ticks validated, by consumer and result.",
               [({"consumer": n, "result": r}, v[r]) for n, v in validation.items() for r in ("passed", "rejected")])
    out.metric("market_validation_rejections_total", "counter",
               "Rejected ticks by the rule they broke (a tick breaking several counts under each).",
               [({"consumer": n, "rule": rule}, count)
                for n, v in validation.items() for rule, count in v["by_rule"].items()])

    agg = snapshot["aggregator"]
    out.metric("market_aggregator_flushes_total", "counter",
               "Candle/repo writes to the database.", [(None, agg["flushes"])])
    if agg["last_flush_at"] is not None:
        out.metric("market_aggregator_last_flush_timestamp_seconds", "gauge",
                   "When the last flush finished, as a Unix timestamp.",
                   [(None, datetime.fromisoformat(agg["last_flush_at"]).timestamp())])
        out.metric("market_aggregator_last_flush_duration_seconds", "gauge",
                   "How long the last flush took.", [(None, agg["last_flush_seconds"])])
        out.metric("market_aggregator_last_flush_rows", "gauge",
                   "Rows written by the last flush.", [(None, agg["last_flush_rows"])])

    feeds = snapshot["feeds"]
    out.metric("market_feed_up", "gauge", "1 while the feed is connected.",
               [({"feed": n}, 1 if f["up"] else 0) for n, f in feeds.items()])
    out.metric("market_feed_reconnects_total", "counter",
               "Times the feed recovered after dropping.", [({"feed": n}, f["reconnects"]) for n, f in feeds.items()])
    out.metric("market_feed_reconnect_attempt", "gauge",
               "Failed attempts in the current outage (0 when up).", [({"feed": n}, f["attempt"]) for n, f in feeds.items()])
    return out.text()
