from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Invoice:
    """The cash for a trade of `face` nominal: the principal at the clean
    price, the accrued interest, and their total. In the security's
    currency."""
    settlement_date: date
    face: float
    clean_price: float     # per 100 face
    days_accrued: int
    principal: float
    accrued: float
    total: float
