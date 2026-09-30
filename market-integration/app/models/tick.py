"""
What flows through the pipeline: an equity quote, a bill or bond quote,
or a bond's repo (sell/buy-back) trades, told apart by `tick_type`.

The buffer, validator, processor, aggregator and gateway all carry
`Tick`; the few places that need a particular kind branch on
`tick_type` (or isinstance). TICK_ADAPTER parses a dict or JSON of any
kind into the right model.
"""

from typing import Annotated, Union

from pydantic import Field, TypeAdapter

from app.models.fixed_income import FixedIncomeTick, RepoTick
from app.models.market_data import MarketData

Tick = Annotated[Union[MarketData, FixedIncomeTick, RepoTick], Field(discriminator="tick_type")]

TICK_ADAPTER: TypeAdapter[Tick] = TypeAdapter(Tick)
