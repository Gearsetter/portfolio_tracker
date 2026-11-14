"""Plain data model shared by the parsers, the archive, the calculators and the Excel writer.

Sign / unit conventions used everywhere in this code base (they match the spreadsheet):

* ``quantity`` is signed: purchases are positive, sales are negative.
* ``fee`` / ``tax_fee`` are **positive numbers that represent a cost** (IBKR reports them as
  negative numbers; the parsers flip the sign).  ``Gross = Total + Fee``.
* ``currency`` is the ISO code taken from the currency sub-header of the statement section.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional, Union, get_args, get_origin, get_type_hints


# --------------------------------------------------------------------------------------
# Transaction level records
# --------------------------------------------------------------------------------------
@dataclass
class StockTrade:
    ticker: str
    name: str
    quantity: float          # signed, sells negative
    price: float             # IBKR "T. Price"
    proceeds: float          # IBKR "Proceeds" (buys negative) - only used for sanity checks
    fee: float               # positive = cost
    currency: str
    dt: datetime
    asset_class: str = "Stocks"
    codes: str = ""
    precision: str = "full"  # "full" (CSV) or "rounded" (HTML shows 2 decimals for amounts)
    account: str = ""        # filled in when the statements are merged (the consolidated view needs it)


@dataclass
class FxTrade:
    """A currency conversion, e.g. symbol ``USD.HUF``: ``bought='USD'`` is paid for with ``paid='HUF'``."""
    bought: str
    paid: str
    quantity: float          # signed units of ``bought`` (negative = the currency was sold)
    price: float             # units of ``paid`` per 1 unit of ``bought``
    proceeds: float          # IBKR proceeds in ``paid`` currency (negative when paid)
    fee: float               # positive = cost
    fee_currency: str
    dt: datetime
    codes: str = ""
    precision: str = "full"
    account: str = ""


@dataclass
class CashFlow:
    """Deposits, withdrawals, non-dividend fees and interest (all in the cash currency)."""
    kind: str                # deposit | withdrawal | monthly fee | interest
    amount: float            # signed: + cash in / - cash out
    currency: str
    dt: datetime
    description: str = ""
    precision: str = "full"
    account: str = ""


@dataclass
class DividendLine:
    """One raw line of the Dividends / Withholding Tax / dividend-Fees sections."""
    kind: str                # dividend | tax | fee
    ticker: str
    isin: str
    amount: float            # signed as reported (tax and fee lines are negative)
    currency: str
    dt: date
    description: str
    key: str                 # normalised description, used to pair dividend <-> tax <-> fee
    precision: str = "full"


@dataclass
class Accrual:
    ticker: str
    currency: str
    dt: date
    ex_date: Optional[date]
    pay_date: Optional[date]
    quantity: float
    gross_rate: float
    gross_amount: float
    net_amount: float
    code: str = ""


@dataclass
class DividendEvent:
    """A dividend with its withholding tax and fees already synchronised (Section 2 row)."""
    ticker: str
    name: str
    quantity: float
    per_share: float
    total: float             # gross amount credited
    tax_fee: float           # withholding tax + dividend fees, positive = cost
    currency: str
    dt: date
    quantity_source: str = ""   # accrual | position | rate | none
    account: str = ""


@dataclass
class Instrument:
    ticker: str
    name: str
    asset_class: str = "Stocks"
    conid: str = ""
    isin: str = ""
    exchange: str = ""
    type: str = ""
    multiplier: float = 1.0


@dataclass
class OpenPosition:
    ticker: str
    currency: str
    quantity: float
    cost_basis: float
    close_price: float
    value: float
    asset_class: str = "Stocks"


@dataclass
class CashLine:
    currency: str            # "Base Currency Summary" or an ISO code
    line: str                # e.g. "Ending Cash"
    total: float


@dataclass
class ParsedStatement:
    filename: str
    fmt: str                 # csv | html
    sha256: str
    account: str
    alias: str
    holder: str
    base_currency: str
    period_start: Optional[date]
    period_end: Optional[date]
    generated: str = ""
    precision: str = "full"
    trades: list[StockTrade] = field(default_factory=list)
    fx_trades: list[FxTrade] = field(default_factory=list)
    cash_flows: list[CashFlow] = field(default_factory=list)
    div_lines: list[DividendLine] = field(default_factory=list)
    accruals: list[Accrual] = field(default_factory=list)
    instruments: list[Instrument] = field(default_factory=list)
    open_positions: list[OpenPosition] = field(default_factory=list)
    cash_report: list[CashLine] = field(default_factory=list)
    nav: dict[str, float] = field(default_factory=dict)          # cash / stock / total (base currency)
    twr_pct: Optional[float] = None
    fx_closes: dict[str, float] = field(default_factory=dict)    # ccy -> base currency per 1 unit at period end
    warnings: list[str] = field(default_factory=list)
    unsupported: dict[str, int] = field(default_factory=dict)    # section/asset class -> ignored rows
    notes: list[str] = field(default_factory=list)               # informational (not a problem)
    # True when the statement contains an "Open Positions" section (even one without stocks).  None = archived
    # before this flag existed: then "has stock rows" is the best available answer (see ``has_open_positions``).
    open_positions_section: Optional[bool] = None

    @property
    def has_open_positions(self) -> bool:
        """Does the statement tell us what is held?  (Its Open Positions section is the source of truth for the
        share counts - see :mod:`ibkr_tracker.portfolio`.)"""
        if self.open_positions_section is not None:
            return self.open_positions_section
        return bool(self.open_positions)


@dataclass
class StatementInfo:
    """Light-weight description of an imported statement (archive listing)."""
    id: int
    filename: str
    fmt: str
    account: str
    alias: str
    period_start: Optional[date]
    period_end: Optional[date]
    imported_at: str
    n_trades: int = 0
    n_other: int = 0


# --------------------------------------------------------------------------------------
# (De)serialisation helpers - the archive stores each parsed statement as JSON
# --------------------------------------------------------------------------------------
def _to_jsonable(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, datetime):
        return obj.isoformat(sep=" ")
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    return obj


def _convert(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(tp)
    if origin is Union:
        args = [a for a in get_args(tp) if a is not type(None)]
        return _convert(args[0], value) if args else value
    if origin in (list, tuple):
        (elem,) = get_args(tp)[:1]
        return [_convert(elem, v) for v in value]
    if origin is dict:
        _, vt = get_args(tp)
        return {k: _convert(vt, v) for k, v in value.items()}
    if tp is datetime:
        return datetime.fromisoformat(value)
    if tp is date:
        return date.fromisoformat(value)
    if dataclasses.is_dataclass(tp):
        return from_dict(tp, value)
    return value


def from_dict(cls: type, data: dict[str, Any]) -> Any:
    hints = get_type_hints(cls)
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name in data:
            kwargs[f.name] = _convert(hints[f.name], data[f.name])
    return cls(**kwargs)


def statement_to_json(ps: ParsedStatement) -> str:
    return json.dumps(_to_jsonable(ps), ensure_ascii=False)


def statement_from_json(text: str) -> ParsedStatement:
    return from_dict(ParsedStatement, json.loads(text))
