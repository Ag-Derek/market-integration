"""
Keeps the economic calendar's pulled sources current: BoG's policy rate
decisions and GSS's release calendar, each pulled and stored on its own,
so one site being down doesn't hold up the other. refresh() never
raises: a failure is logged and kept for /health, what's stored stays in
use, and the caller tries again later (main.econ_loop).
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

from app.econ import GssRelease, PolicyDecision
from app.econ.store import EconStore

logger = logging.getLogger(__name__)

RETRY_SECONDS = 600


class PolicySource(Protocol):
    def fetch(self) -> list[PolicyDecision]: ...


class ReleaseSource(Protocol):
    def fetch(self) -> list[GssRelease]: ...


@dataclass
class _Pull:
    """One source's last pull."""
    last_attempt_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_error: Optional[str] = None

    def describe(self) -> dict:
        return {
            "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "last_error": self.last_error,
        }


class EconTracker:
    def __init__(self, store: EconStore, policy: PolicySource, releases: ReleaseSource):
        self._store = store
        self._sources = {"policy_rate": policy, "gss": releases}
        self._pulls = {name: _Pull() for name in self._sources}
        self.decisions: list[PolicyDecision] = []
        self.releases: list[GssRelease] = []

    def reload(self) -> None:
        """Re-read the store. Blocking: SQLite."""
        self.decisions = self._store.decisions()
        self.releases = self._store.releases()

    async def refresh(self) -> bool:
        """Pull both sources; True if both succeeded."""
        ok = True
        for name, save in (("policy_rate", self._store.upsert_decisions), ("gss", self._store.replace_releases)):
            ok &= await self._pull(name, save)
        await asyncio.to_thread(self.reload)
        return ok

    async def _pull(self, name: str, save: Callable) -> bool:
        pull = self._pulls[name]
        pull.last_attempt_at = datetime.now(timezone.utc)
        try:
            rows = await asyncio.to_thread(self._sources[name].fetch)
            await asyncio.to_thread(save, rows)
        except Exception as e:
            pull.last_error = f"{type(e).__name__}: {e}"
            logger.warning("Economic calendar: %s pull failed (still using what's stored): %s", name, pull.last_error)
            return False
        pull.last_success_at = datetime.now(timezone.utc)
        pull.last_error = None
        logger.info("Economic calendar: %s refreshed, %d rows", name, len(rows))
        return True

    @property
    def policy_rate(self) -> Optional[PolicyDecision]:
        """The latest decision: the rate in force."""
        return self.decisions[-1] if self.decisions else None

    def describe(self) -> dict:
        """Where each source stands, for /health and the calendar."""
        return {
            "policy_rate": {"decisions": len(self.decisions),
                            "latest": self.policy_rate.date.isoformat() if self.policy_rate else None,
                            **self._pulls["policy_rate"].describe()},
            "gss": {"releases": len(self.releases), **self._pulls["gss"].describe()},
        }
