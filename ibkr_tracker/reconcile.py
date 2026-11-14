"""Reconciliation of the workbook figures with the reference numbers printed in the newest IBKR statement.

The *Checks* sheet of the workbook implements the same rules with formulas; the tests assert that both agree.
"""
from __future__ import annotations

from dataclasses import dataclass

from .calc import Metrics
from .portfolio import Portfolio

NAV_REL_TOL = 0.005          # holdings are valued at the latest prices, IBKR's NAV at the statement date
QTY_TOL = 1e-6


@dataclass
class CheckRow:
    check: str               # Cash balance | Open position (qty) | Net asset value
    item: str
    ibkr: float
    workbook: float
    difference: float
    tolerance: float
    status: str              # OK | CHECK | INFO


def cash_tolerance(p: Portfolio) -> float:
    """0.01 for full-precision data, plus 0.0051 for every amount IBKR's HTML rendering rounded to cents."""
    n_rounded = sum(1 for x in [*p.trades, *p.fx_trades, *p.cash_flows] if x.precision != "full")
    return round(0.01 + 0.0051 * n_rounded, 4)


def reconcile(p: Portfolio, m: Metrics) -> list[CheckRow]:
    latest = p.latest
    rows: list[CheckRow] = []
    if latest is None:
        return rows
    tol = cash_tolerance(p)
    ending = {l.currency: l.total for l in latest.cash_report if l.line == "Ending Cash"}
    for c in m.cash:
        if c.currency in ending:
            diff = c.balance - ending[c.currency]
            rows.append(CheckRow("Cash balance", c.currency, ending[c.currency], c.balance, diff, tol,
                                 "OK" if abs(diff) <= tol else "CHECK"))
    held = {(op.ticker, op.currency): op.quantity for op in latest.open_positions}
    for pos in m.positions:
        ibkr_qty = held.get((pos.ticker, pos.currency), 0.0)
        diff = pos.quantity - ibkr_qty
        rows.append(CheckRow("Open position (qty)", pos.ticker, ibkr_qty, pos.quantity, diff, QTY_TOL,
                             "OK" if abs(diff) <= QTY_TOL else "CHECK"))
    if "total" in latest.nav:
        nav = latest.nav["total"]
        diff = m.base_total - nav
        tol_nav = NAV_REL_TOL * nav
        rows.append(CheckRow(f"Net asset value ({m.base_currency})", m.base_currency, nav, m.base_total, diff, tol_nav,
                             "OK" if abs(diff) <= tol_nav else "INFO"))
    return rows


def overall(rows: list[CheckRow]) -> str:
    bad = sum(1 for r in rows if r.status == "CHECK")
    return "all checks passed" if bad == 0 else f"{bad} check(s) need review"
