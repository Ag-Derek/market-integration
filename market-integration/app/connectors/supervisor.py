"""
Keeps a connector's feed up: when it drops, reconnect with exponential
backoff and jitter, without restarting the service.

    feed = SupervisedConnector(SomeConnector(...), name="equities")
    await feed.connect()
    async for tick in feed.stream():   # never ends while the service runs
        ...

stream() wraps the connector's own. When that raises, or ends (a live
feed doesn't end; one that does has dropped), the supervisor
disconnects the connector, waits, connects it again and carries on
streaming. A failed connect() is retried the same way. Retry n waits

    min(max_delay, initial_delay * 2**(n-1)), less up to `jitter` of it

so the first retry is quick, a long outage backs off to one attempt per
max_delay, and the randomised part keeps many clients that dropped
together from hammering the provider in step. The count resets once
ticks flow again, not merely on a successful connect(), so a provider
that accepts connections and then drops them straight away still backs
off.

Everything else (symbols, running, last_heartbeat, fetch_history) is
the connector's own. `state` says what the supervisor is doing, for
/health and the status badge; `on_change` is called whenever it
changes, so clients can be told at once rather than at the next status
tick.

Only disconnect() on the supervisor stops it for good (shutdown).
Disconnecting the wrapped connector directly counts as a drop.
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Awaitable, Callable, Optional

from app.connectors.base_connector import BaseMarketConnector
from app.models.candle import Candle
from app.models.tick import Tick

logger = logging.getLogger(__name__)

CONNECTING = "connecting"
CONNECTED = "connected"
RECONNECTING = "reconnecting"
STOPPED = "stopped"


class SupervisedConnector(BaseMarketConnector):

    def __init__(
        self,
        connector: BaseMarketConnector,
        *,
        name: str,
        initial_delay: float = 1.0,
        max_delay: float = 30.0,
        jitter: float = 0.5,
        on_change: Optional[Callable[["SupervisedConnector"], None]] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_fraction: Callable[[], float] = random.random,
    ):
        # Not BaseMarketConnector.__init__: symbols, running and
        # last_heartbeat are the wrapped connector's.
        if initial_delay <= 0 or max_delay < initial_delay:
            raise ValueError(f"need 0 < initial_delay <= max_delay, got {initial_delay}, {max_delay}")
        if not 0 <= jitter <= 1:
            raise ValueError(f"jitter must be between 0 and 1, got {jitter}")
        self.connector = connector
        self.name = name
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.jitter = jitter
        self.on_change = on_change
        self._sleep = sleep
        self._random = random_fraction

        self.state = CONNECTING
        self.attempt = 0                       # failed attempts since ticks last flowed
        self.next_retry_at: Optional[datetime] = None
        self.last_error: Optional[str] = None
        self.reconnects = 0                    # successful recoveries, ever
        self._stopping = False

    # ------------------------------------------------------------ delegated

    @property
    def symbols(self) -> list[str]:
        return self.connector.symbols

    @property
    def running(self) -> bool:
        return self.connector.running

    @property
    def last_heartbeat(self) -> Optional[datetime]:
        return self.connector.last_heartbeat

    async def fetch_history(
        self, symbol: str, interval: str, start: Optional[datetime] = None
    ) -> list[Candle]:
        return await self.connector.fetch_history(symbol, interval, start)

    async def connect(self) -> None:
        self._stopping = False
        await self.connector.connect()
        self._set_state(CONNECTED)

    async def disconnect(self) -> None:
        self._stopping = True
        self._set_state(STOPPED)
        await self.connector.disconnect()

    # ------------------------------------------------------------ status

    @property
    def reconnecting(self) -> bool:
        return self.state in (RECONNECTING, CONNECTING) and self.attempt > 0

    def describe(self) -> dict:
        """The supervisor's state, as /health and the status badge show it."""
        return {
            "state": self.state,
            "attempt": self.attempt,
            "next_retry_at": self.next_retry_at.isoformat() if self.next_retry_at else None,
            "last_error": self.last_error,
            "reconnects": self.reconnects,
        }

    def _set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        if self.on_change is not None:
            try:
                self.on_change(self)
            except Exception:
                logger.exception("%s feed: state-change callback failed", self.name)

    def backoff(self, attempt: int) -> float:
        """How long to wait before retry number `attempt` (1-based)."""
        delay = min(self.max_delay, self.initial_delay * 2 ** (attempt - 1))
        return delay * (1 - self.jitter * self._random())

    # ------------------------------------------------------------ stream

    async def stream(self) -> AsyncIterator[Tick]:
        while not self._stopping:
            if not self.connector.running:
                self._set_state(CONNECTING)
                try:
                    await self.connector.connect()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    await self._wait_to_retry(f"connect failed: {e!r}")
                    continue

            self._set_state(CONNECTED)
            error = "stream ended"
            try:
                async for tick in self.connector.stream():
                    if self.attempt:
                        logger.info("%s feed: recovered after %d failed attempt(s)", self.name, self.attempt)
                        self.attempt = 0
                        self.next_retry_at = None
                        self.reconnects += 1
                    yield tick
            except asyncio.CancelledError:
                raise
            except Exception as e:
                error = f"stream failed: {e!r}"

            if self._stopping:
                return
            try:
                await self.connector.disconnect()
            except Exception:
                logger.exception("%s feed: disconnect after a drop failed", self.name)
            await self._wait_to_retry(error)

    async def _wait_to_retry(self, error: str) -> None:
        self.attempt += 1
        self.last_error = error
        delay = self.backoff(self.attempt)
        self.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        logger.warning(
            "%s feed dropped (%s); reconnect attempt %d in %.1fs",
            self.name, error, self.attempt, delay,
        )
        self._set_state(RECONNECTING)
        await self._sleep(delay)
