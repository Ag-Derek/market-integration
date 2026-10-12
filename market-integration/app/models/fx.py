from datetime import date

from pydantic import BaseModel, ConfigDict


class FxRate(BaseModel):
    """A Bank of Ghana interbank fix for one currency, as cedis per unit
    of it (whichever way BoG quotes the pair; see app/fx)."""

    model_config = ConfigDict(frozen=True)

    currency: str        # ISO 4217, e.g. USD
    date: date           # the business day BoG published it for
    mid: float           # GHS per unit: the rate we convert at
    buying: float
    selling: float
    source_pair: str     # as BoG publishes it, e.g. USDGHS or GHSNGN

    @property
    def per_ghs(self) -> float:
        """Units of the currency one cedi buys, at the mid."""
        return 1 / self.mid
