"""Market data containers shared by the calculators, the Excel writer and the GUI.

FX convention used everywhere: **units of currency X per 1 USD** (``HUF`` ~ 320, ``EUR`` ~ 0.86) - the same
orientation as IBKR's ``USD.HUF`` and Yahoo's ``HUF=X``.  Converting an amount to USD is therefore
``amount / rate``.  As-of lookups mirror the spreadsheet (``MATCH(date, dates, 1)`` falling back to the first
row when the date precedes the data).
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass
class PricePoint:
    price: float
    day: date
    source: str
    symbol: str = ""


SRC_MANUAL = "manual"            # typed / pasted / uploaded price history (highest priority)
SRC_YAHOO = "yahoo"              # downloaded and cached daily closes
SRC_STATEMENT = "statement"      # closing prices printed in the IBKR statements (period ends only)
SOURCE_LABEL = {SRC_MANUAL: "Manual price history", SRC_YAHOO: "Yahoo Finance", SRC_STATEMENT: "IBKR statement"}


@dataclass
class PriceSeries:
    """Daily closes of one stock in its trading currency, merged from every source (priority per day: manual price
    history > Yahoo Finance > statement).  Prices are split-adjusted, like Yahoo's ``Close``."""
    dates: list[date] = field(default_factory=list)
    closes: list[float] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)            # SRC_* of every point
    symbol: str = ""

    def __len__(self) -> int:
        return len(self.dates)

    def asof(self, d: date) -> Optional[tuple[date, float, str]]:
        """``(day, close, source)`` of the last point on or before ``d``; ``None`` before the first point."""
        i = bisect_right(self.dates, d) - 1
        if i < 0:
            return None
        return self.dates[i], self.closes[i], self.sources[i]

    @property
    def first(self) -> Optional[date]:
        return self.dates[0] if self.dates else None

    @property
    def last(self) -> Optional[date]:
        return self.dates[-1] if self.dates else None

    def count(self, source: str) -> int:
        return sum(1 for s in self.sources if s == source)


def merge_series(layers: list[tuple[str, list[tuple[date, float]]]], symbol: str = "") -> PriceSeries:
    """Combine price layers - ``[(SRC_*, [(day, close), ...]), ...]`` with the **highest priority first** - into one
    series: on a day that several layers cover, the first layer wins."""
    chosen: dict[date, tuple[float, str]] = {}
    for source, rows in layers:
        for d, v in rows:
            if d not in chosen:
                chosen[d] = (float(v), source)
    days = sorted(chosen)
    return PriceSeries(days, [chosen[d][0] for d in days], [chosen[d][1] for d in days], symbol)


@dataclass
class FxSeries:
    dates: list[date]
    rates: list[float]                  # units of the currency per 1 USD
    source: str = ""

    def asof(self, d: date) -> float:
        i = bisect_right(self.dates, d) - 1
        return self.rates[max(i, 0)]

    @property
    def last(self) -> tuple[float, date]:
        return self.rates[-1], self.dates[-1]


class FxTable:
    def __init__(self, series: Optional[dict[str, FxSeries]] = None):
        self.series: dict[str, FxSeries] = {k: v for k, v in (series or {}).items() if v.dates}

    def currencies(self) -> list[str]:
        return list(self.series)

    def has(self, ccy: str) -> bool:
        return ccy == "USD" or ccy in self.series

    def rate(self, ccy: str, d: date) -> float:
        """Units of ``ccy`` per 1 USD on ``d`` (last known value on/before ``d``, first value if earlier)."""
        if ccy == "USD":
            return 1.0
        return self.series[ccy].asof(d)

    def latest(self, ccy: str) -> tuple[float, date]:
        if ccy == "USD":
            return 1.0, date.today()
        return self.series[ccy].last

    def to_usd(self, amount: float, ccy: str, d: date) -> float:
        return amount / self.rate(ccy, d)

    def union_dates(self) -> list[date]:
        out: set[date] = set()
        for s in self.series.values():
            out.update(s.dates)
        return sorted(out)

    def matrix(self, ccys: list[str]) -> list[tuple[date, list[float]]]:
        """Dense table (union of dates, per-currency as-of values) - exactly what the FX sheet holds."""
        dates = self.union_dates()
        return [(d, [self.series[c].asof(d) for c in ccys]) for d in dates]


@dataclass
class BenchSeries:
    symbol: str = ""
    currency: str = "USD"
    dates: list[date] = field(default_factory=list)
    usd: list[float] = field(default_factory=list)       # close converted to USD
    native: list[float] = field(default_factory=list)    # close in the quote currency
    source: str = ""

    def close(self, d: date) -> Optional[float]:
        if not self.dates:
            return None
        i = bisect_right(self.dates, d) - 1
        return self.usd[max(i, 0)]

    @property
    def latest(self) -> Optional[PricePoint]:
        if not self.dates:
            return None
        return PricePoint(self.usd[-1], self.dates[-1], self.source or "Yahoo Finance", self.symbol)


@dataclass
class MarketData:
    prices: dict[str, PricePoint]                 # "TICKER|CCY" -> price used (overrides applied)
    raw_prices: dict[str, PricePoint]             # before manual overrides
    price_overrides: dict[str, float]
    fx: FxTable
    fx_latest_raw: dict[str, PricePoint]          # ccy -> latest data point (before overrides)
    fx_overrides: dict[str, float]
    benchmark: BenchSeries
    benchmark_override: Optional[float]
    as_of: date
    mode: str
    yahoo_symbols: dict[str, str] = field(default_factory=dict)
    missing_fx: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    last_refresh: str = ""
    # full daily history of every stock ("TICKER|CCY" -> merged series); feeds the day-by-day reconstruction
    price_series: dict[str, PriceSeries] = field(default_factory=dict)
    manual_keys: list[str] = field(default_factory=list)        # stocks that have a manual price history

    # ---- accessors ---------------------------------------------------------------------------------------
    def price(self, ticker: str, ccy: str) -> Optional[float]:
        p = self.prices.get(f"{ticker}|{ccy}")
        return p.price if p else None

    def fx_used(self, ccy: str) -> float:
        """Latest units of ``ccy`` per USD (manual override wins)."""
        if ccy == "USD":
            return 1.0
        if self.fx_overrides.get(ccy):
            return float(self.fx_overrides[ccy])
        if ccy in self.fx_latest_raw:
            return self.fx_latest_raw[ccy].price
        return self.fx.latest(ccy)[0]

    def benchmark_price(self) -> Optional[float]:
        if self.benchmark_override:
            return float(self.benchmark_override)
        latest = self.benchmark.latest
        return latest.price if latest else None
