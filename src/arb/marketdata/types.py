from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Market:
  """Exchange-agnostic market. Adapters map venue symbols onto this."""

  exchange: str
  base: str
  quote: str

  @property
  def symbol(self) -> str:
    return f"{self.base}-{self.quote}"


@dataclass(frozen=True, slots=True)
class Level:
  price: Decimal
  size: Decimal
