"""Statement-derived market data - used when Yahoo Finance is unreachable (or switched off).

* Stock prices: the *Close Price* of the ``Open Positions`` section at each statement's period end.
* FX: IBKR's own conversion prices.  The 17:00:00 end-of-day conversions are IBKR's closing rate; on days
  without them the average AutoFX trade price is used.  The period-end ``Mark-to-Market`` forex prices add the
  closing rate of the statement date.  Cross rates (e.g. EUR) are derived through the traded pairs.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, time

from ..portfolio import Portfolio
from .data import FxSeries, PricePoint


def statement_prices(portfolio: Portfolio) -> dict[str, PricePoint]:
    out: dict[str, PricePoint] = {}
    for s in portfolio.statements:                   # ascending by period end: newer statements overwrite
        if not s.period_end:
            continue
        for op in s.open_positions:
            if op.close_price:
                out[f"{op.ticker}|{op.currency}"] = PricePoint(
                    op.close_price, s.period_end, f"IBKR statement {s.period_end:%Y-%m-%d}", op.ticker)
    return out


def _solve_units_per_usd(rates: dict[tuple[str, str], float]) -> dict[str, float]:
    """``rates[(A, B)] = units of B per 1 A``  ->  units of each currency per 1 USD (graph walk from USD)."""
    units = {"USD": 1.0}
    changed = True
    while changed:
        changed = False
        for (a, b), p in rates.items():
            if p <= 0:
                continue
            if a in units and b not in units:
                units[b] = units[a] * p
                changed = True
            elif b in units and a not in units:
                units[a] = units[b] / p
                changed = True
    return units


def statement_fx(portfolio: Portfolio) -> dict[str, FxSeries]:
    per_day: dict[date, dict[tuple[str, str], list[tuple[float, bool]]]] = defaultdict(lambda: defaultdict(list))
    for f in portfolio.fx_trades:
        if f.price > 0:
            per_day[f.dt.date()][(f.bought, f.paid)].append((f.price, f.dt.time() == time(17, 0, 0)))

    rows: dict[str, dict[date, float]] = defaultdict(dict)
    for d, pairs in per_day.items():
        rates: dict[tuple[str, str], float] = {}
        for pair, lst in pairs.items():
            closing = [p for p, is_close in lst if is_close]
            use = closing or [p for p, _ in lst]
            rates[pair] = sum(use) / len(use)
        for ccy, u in _solve_units_per_usd(rates).items():
            if ccy != "USD":
                rows[ccy][d] = u

    for s in portfolio.statements:                    # authoritative period-end closing rates
        if s.period_end and s.fx_closes.get("USD"):
            usd_in_base = s.fx_closes["USD"]
            for ccy, price_in_base in s.fx_closes.items():
                if ccy != "USD" and price_in_base:
                    rows[ccy][s.period_end] = usd_in_base / price_in_base

    out: dict[str, FxSeries] = {}
    for ccy, by_day in rows.items():
        days = sorted(by_day)
        out[ccy] = FxSeries(days, [by_day[d] for d in days], "IBKR statements (approximate)")
    return out
