"""Hand-made :class:`MarketData` - exact prices and rates, no provider, no store - for tests that compare the
day-by-day history with numbers computed by hand."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from ibkr_tracker.market.data import (SRC_MANUAL, SRC_STATEMENT, SRC_YAHOO, BenchSeries, FxSeries, FxTable,
                                      MarketData, PricePoint, merge_series)
from ibkr_tracker.portfolio import Portfolio


def daily(first: date, last: date, fn, weekdays_only: bool = False) -> list[tuple[date, float]]:
    """``[(day, fn(day)), ...]`` for every day (or every weekday) from ``first`` to ``last``."""
    out, d = [], first
    while d <= last:
        if not weekdays_only or d.weekday() < 5:
            out.append((d, float(fn(d))))
        d += timedelta(days=1)
    return out


def make_md(portfolio: Portfolio, *, prices: Optional[dict[str, list]] = None, manual: Optional[dict[str, list]] = None,
            statement: Optional[dict[str, list]] = None, fx: Optional[dict[str, list]] = None,
            bench: Optional[list] = None, as_of: Optional[date] = None, price_over: Optional[dict] = None,
            fx_over: Optional[dict] = None, bench_over: Optional[float] = None,
            bench_symbol: str = "VUAA.L") -> MarketData:
    """``prices`` / ``manual`` / ``statement``: ``{"TICKER|CCY": [(day, close), ...]}`` (the three layers of the price
    series, manual first); ``fx``: ``{"EUR": [(day, units per USD), ...]}``; ``bench``: ``[(day, USD close), ...]``."""
    prices, manual, statement = prices or {}, manual or {}, statement or {}
    series, raw = {}, {}
    for t, c in portfolio.positions_keys():
        key = f"{t}|{c}"
        ser = merge_series([(SRC_MANUAL, manual.get(key, [])), (SRC_YAHOO, prices.get(key, [])),
                            (SRC_STATEMENT, statement.get(key, []))], key)
        if len(ser):
            series[key] = ser
            raw[key] = PricePoint(ser.closes[-1], ser.dates[-1], ser.sources[-1], key)
    over = dict(price_over or {})
    shown = dict(raw)
    for key, v in over.items():
        base = raw.get(key)
        shown[key] = PricePoint(v, base.day if base else (as_of or date.today()), "Manual override", key)
    fx_series = {c: FxSeries([d for d, _ in rows], [r for _, r in rows], "test") for c, rows in (fx or {}).items()}
    latest = {c: PricePoint(s.rates[-1], s.dates[-1], "test", f"{c}=X") for c, s in fx_series.items()}
    bs = BenchSeries(bench_symbol, "USD", [d for d, _ in bench or []], [v for _, v in bench or []],
                     [v for _, v in bench or []], "test")
    held = [k for k, q in portfolio.net_quantities().items() if abs(q) > 1e-9]
    days = [raw[f"{t}|{c}"].day for t, c in held if f"{t}|{c}" in raw]
    return MarketData(prices=shown, raw_prices=raw, price_overrides=over, fx=FxTable(fx_series), fx_latest_raw=latest,
                      fx_overrides=dict(fx_over or {}), benchmark=bs, benchmark_override=bench_over,
                      as_of=as_of or (max(days) if days else (portfolio.period_end or date.today())),
                      mode="test", yahoo_symbols={f"{t}|{c}": t for t, c in portfolio.positions_keys()},
                      price_series=series, manual_keys=[k for k, v in manual.items() if v])


# ---- a hand-made market for the synthetic account (tests/synth.py) --------------------------------------------------
D0, END = date(2024, 12, 30), date(2025, 12, 31)          # first / last day of the hand-made series (Mon / Wed)


def last_wd(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def n_of(d: date) -> int:
    """Days since 2024-12-31 of the last weekday on or before ``d`` - the walk of the prices below is linear in it."""
    return (last_wd(d) - date(2024, 12, 31)).days


def px_aapl(d): return 150 + 0.1 * n_of(d)                # USD
def px_sap(d): return 120 + 0.05 * n_of(d)                # EUR
def px_tsla(d): return 200 + 0.2 * n_of(d)                # USD
def fx_eur(d): return 0.95 if last_wd(d) < date(2025, 6, 2) else 0.90          # EUR per USD
def bench_px(d): return 100 + 0.2 * n_of(d)               # USD


def market(pf: Portfolio, **kw) -> MarketData:
    """Daily weekday prices of every stock of the synthetic account (AAPL, SAP, TSLA), the EUR rate and the benchmark,
    all with the closed-form values above; ``kw`` goes to :func:`make_md` (``prices=`` adds / replaces series)."""
    prices = {"AAPL|USD": daily(D0, END, px_aapl, True), "SAP|EUR": daily(D0, END, px_sap, True),
              "TSLA|USD": daily(D0, END, px_tsla, True)}
    prices = {k: v for k, v in prices.items() if k in {f"{t}|{c}" for t, c in pf.positions_keys()}}
    prices.update(kw.pop("prices", {}))
    args = dict(prices=prices, fx={"EUR": daily(D0, END, fx_eur, True)}, bench=daily(D0, END, bench_px, True),
                as_of=END)
    args.update(kw)
    return make_md(pf, **args)
