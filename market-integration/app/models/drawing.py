"""
Drawings a user puts on a stock chart (app/drawings/, /drawings/me/...):
trend lines, horizontal levels and Fibonacci retracements.

Each is saved as anchors of (time, price), never as screen positions, so
it stays on the same candles and prices whatever the chart's range,
scale or size. Prices are in cedis, the currency the chart's candles are
stored in; the page shows drawings only when the chart is in cedis.
"""

import math
from datetime import timezone
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

DrawingKind = Literal["trend", "level", "fib"]

# Anchors each kind takes: a trend line and a retracement run between two
# points; a level is a price (its time is only where it was drawn).
ANCHORS: dict[str, int] = {"trend": 2, "level": 1, "fib": 2}
MAX_DRAWINGS = 100   # per user per symbol


class Anchor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    t: AwareDatetime                     # when, on the chart's time axis
    p: float = Field(gt=0, lt=1e9)       # price, in cedis

    @field_validator("t")
    @classmethod
    def _utc(cls, t):
        return t.astimezone(timezone.utc)

    @field_validator("p")
    @classmethod
    def _finite(cls, p):
        if not math.isfinite(p):
            raise ValueError("price must be a finite number")
        return p


class Drawing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")   # chosen by the page, to edit it later
    kind: DrawingKind
    points: list[Anchor]
    extend: bool = False                 # trend lines: carry on past the second point to the chart's edge

    @model_validator(mode="after")
    def _shape(self) -> "Drawing":
        want = ANCHORS[self.kind]
        if len(self.points) != want:
            raise ValueError(f"a {self.kind} drawing takes {want} point{'s' if want > 1 else ''}, "
                             f"got {len(self.points)}")
        if self.kind == "trend" and self.points[0].t == self.points[1].t:
            raise ValueError("a trend line's two points must be at different times")
        if self.kind == "fib" and self.points[0].p == self.points[1].p:
            raise ValueError("a retracement's two points must be at different prices")
        if self.extend and self.kind != "trend":
            raise ValueError("only trend lines extend")
        return self


class DrawingsUpdate(BaseModel):
    """PUT /drawings/me/{symbol}: every drawing on that chart, replacing
    what was saved."""
    model_config = ConfigDict(extra="forbid")

    drawings: list[Drawing] = Field(max_length=MAX_DRAWINGS)

    @model_validator(mode="after")
    def _unique_ids(self) -> "DrawingsUpdate":
        ids = [d.id for d in self.drawings]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"drawing ids must be unique; repeated: {', '.join(dupes)}")
        return self
