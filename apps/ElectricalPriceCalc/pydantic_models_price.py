from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class PeakHour(BaseModel):
    """A continuous block of slots to save (or spend) in, as handed to consumers."""
    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    duration: timedelta


class PriceHour(BaseModel):
    """One price slot (hourly or 15 min) with all taxes/support applied."""
    model_config = ConfigDict(frozen=True)

    start: datetime
    end: datetime
    value: float


# A tax is either one flat value or a value per month (keys 1..12).
MonthTax = float | dict[int, float]

_ALL_MONTHS = frozenset(range(1, 13))
_PRICE_SOURCES = ('fixedprice', 'pricearea', 'nordpool')


class PriceCalcConfig(BaseModel):
    """Validated view of the app's apps.yaml arguments.

    Defaults are identical to the pre-0.1.8 ``self.args.get(...)`` calls. Unknown keys
    (``module``, ``class``, ``dependencies``, ...) are ignored. Mode precedence stays
    fixedprice > pricearea > nordpool > auto-discover, but giving more than one source
    is now rejected instead of silently picking the first.
    """
    model_config = ConfigDict(extra='ignore', frozen=True)

    fixedprice: float | None = None
    pricearea: str | None = None
    nordpool: str | None = None
    currency: str = 'EUR'
    # VAT multiplier applied to raw Nordpool spot prices only (pricearea mode). Defaults to
    # 1.25 for pricearea and 0 otherwise, see _default_vat.
    VAT: float = 0.0
    daytax: MonthTax = 0.0
    nighttax: MonthTax = 0.0
    additional_tax: float = 0.0
    power_support_above: float = 10.0
    support_amount: float = 0.0
    country_code: str | None = None

    @model_validator(mode='before')
    @classmethod
    def _default_vat(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get('VAT') is None:
            data = dict(data)
            data['VAT'] = 1.25 if data.get('pricearea') is not None else 0.0
        return data

    @field_validator('daytax', 'nighttax')
    @classmethod
    def _all_months_present(cls, value: MonthTax) -> MonthTax:
        if isinstance(value, dict):
            missing = sorted(_ALL_MONTHS - set(value))
            if missing:
                raise ValueError(f"missing month keys {missing}; a tax dict must cover months 1..12")
        return value

    @model_validator(mode='after')
    def _at_most_one_price_source(self) -> 'PriceCalcConfig':
        given = [name for name in _PRICE_SOURCES if getattr(self, name) is not None]
        if len(given) > 1:
            raise ValueError(f"only one of {_PRICE_SOURCES} may be configured, got {given}")
        return self
