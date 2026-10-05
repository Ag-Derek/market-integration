"""
Pipeline metrics behind GET /metrics: how much the buffer is dropping
and holding, how many WebSocket clients are connected, what validation
turns away (by rule), the aggregator's last database write and the
candles it flagged possibly incomplete, and each feed's reconnects.

collect() reads the counters the components already keep into one
snapshot (served as JSON); to_prometheus() renders that snapshot in the
Prometheus text exposition format, so a Prometheus server can scrape
/metrics directly with no client library.

A drop on a lossless subscriber (the aggregator; alerts) means data
loss, so alert on

    increase(market_buffer_dropped_ticks_total{subscriber="aggregator"}[5m]) > 0

The display branch ("processor") is conflated: it skips ticks a newer
one replaced before it read them, which is expected and counted as
"conflated", not "dropped". So is the WebSocket gateway's per-client
conflation, and its send counters show what clients actually get: at
most one batched tick message per client per send interval (#20).
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
    conflated = buffer.conflated_counts
    # Queue size, high-water mark and time the feed waited on it, for
    # subscribe() subscribers (conflated ones have no queue).
    queued = {
        s["subscriber"]: {
            "capacity": s["capacity"],
            "peak_depth": s["peak_depth"],
            "blocked_seconds": s["blocked_seconds"],
        }
        for s in buffer.stats()
    }
    return {
        "buffer": {
            "received": buffer.received,
            "capacity": buffer.maxsize,
            "subscribers": {
                name: {
                    "mode": mode,
                    "depth": depths.get(name, 0),
                    "dropped": dropped.get(name, 0),
                    **({"conflated": conflated[name]} if name in conflated else {}),
                    **queued.get(name, {}),
                }
                for name, mode in buffer.subscriber_modes.items()
            },
        },
        "websocket": {
            "clients": gateway.client_count,
            "subscriptions": gateway.subscription_count,
            "conflated": gateway.conflated_count,
            "send_interval_seconds": gateway.send_interval,
            "messages_sent": gateway.messages_sent,
            "tick_messages_sent": gateway.tick_messages_sent,
            "ticks_sent": gateway.ticks_sent,
        },
        "validation": {name: counter.snapshot() for name, counter in validators.items()},
        "aggregator": {
            "flushes": aggregator.flushes,
            "last_flush_at": aggregator.last_flush_at.isoformat() if aggregator.last_flush_at else None,
            "last_flush_seconds": aggregator.last_flush_seconds,
            "last_flush_rows": aggregator.last_flush_rows,
            "incomplete_candles": aggregator.incomplete_candles,
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
    queued = {n: s for n, s in subscribers.items() if "capacity" in s}
    out.metric("market_buffer_received_ticks_total", "counter",
               "Ticks taken off the connector by the buffer.", [(None, buffer["received"])])
    out.metric("market_buffer_dropped_ticks_total", "counter",
               "Ticks dropped (oldest first) because a subscriber fell behind.",
               [({"subscriber": n}, s["dropped"]) for n, s in subscribers.items()])
    out.metric("market_buffer_queue_depth", "gauge",
               "Ticks waiting to be read, per subscriber (symbols with an unread tick, for conflated ones).",
               [({"subscriber": n, "mode": s["mode"]}, s["depth"]) for n, s in subscribers.items()])
    out.metric("market_buffer_conflated_ticks_total", "counter",
               "Ticks a conflated subscriber skipped because a newer one for the symbol replaced them.",
               [({"subscriber": n}, s["conflated"]) for n, s in subscribers.items() if "conflated" in s])
    out.metric("market_buffer_queue_peak_depth", "gauge",
               "Most ticks ever waiting in a subscriber's queue at once since startup.",
               [({"subscriber": n}, s["peak_depth"]) for n, s in queued.items()])
    out.metric("market_buffer_queue_capacity", "gauge",
               "Per-subscriber queue size: the display's drops when full, a lossless one's holds the feed.",
               [({"subscriber": n}, s["capacity"]) for n, s in queued.items()])
    out.metric("market_buffer_blocked_seconds_total", "counter",
               "Seconds the feed was held back waiting for a full lossless subscriber.",
               [({"subscriber": n}, s["blocked_seconds"]) for n, s in queued.items()])

    ws = snapshot["websocket"]
    out.metric("market_websocket_clients", "gauge", "Connected WebSocket clients.", [(None, ws["clients"])])
    out.metric("market_websocket_subscriptions", "gauge",
               "Symbol subscriptions across all WebSocket clients.", [(None, ws["subscriptions"])])
    out.metric("market_websocket_conflated_ticks_total", "counter",
               "Ticks never sent because a newer one for the symbol replaced them before the client's next batch.",
               [(None, ws["conflated"])])
    out.metric("market_websocket_messages_sent_total", "counter",
               "WebSocket messages sent to clients, of any type.", [(None, ws["messages_sent"])])
    out.metric("market_websocket_tick_messages_sent_total", "counter",
               "Batched tick messages sent: at most one per client per send interval.",
               [(None, ws["tick_messages_sent"])])
    out.metric("market_websocket_ticks_sent_total", "counter",
               "Quotes sent inside tick messages.", [(None, ws["ticks_sent"])])

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
    out.metric("market_aggregator_incomplete_candles_total", "counter",
               "Live candles flagged possibly_incomplete because a tick was dropped.",
               [(None, agg["incomplete_candles"])])
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
