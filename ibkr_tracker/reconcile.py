"""Reconciliation of the workbook figures with the reference numbers printed in the newest IBKR statement.

The *Checks* sheet of the workbook implements the same rules with formulas; the tests assert that both agree.

The share counts are not reconciled *against* the statement - they are taken from it (Open Positions of the latest
statement).  The "Share count" rows instead show where the sum of the transactions disagrees with the statement (a stock
split, a transfer, history before the first imported statement): that is information (``INFO``), not an error.
"""
from __future__ import annotations

from dataclasses import dataclass

from .calc import Metrics
from .portfolio import QTY_TOL, SRC_TRANSACTIONS, Portfolio

NAV_REL_TOL = 0.005          # holdings are valued at the latest prices, IBKR's NAV at the statement date
SHARE_CHECK = "Share count: statement vs transactions"


@dataclass
class CheckRow:
    check: str               # Cash balance | Share count: statement vs transactions | Net asset value
    item: str
    ibkr: float
    workbook: float
    difference: float
    tolerance: float
    status: str              # OK | CHECK | INFO


def cash_tolerance(p: Portfolio) -> float:
    """0.01 (per account) for full-precision data, plus 0.0051 for every amount IBKR's HTML rendering rounded to
    cents."""
    n_rounded = sum(1 for x in [*p.trades, *p.fx_trades, *p.cash_flows] if x.precision != "full")
    return round(0.01 * max(1, len(p.account_ids)) + 0.0051 * n_rounded, 4)


def reconcile(p: Portfolio, m: Metrics) -> list[CheckRow]:
    refs = p.latest_statements()
    rows: list[CheckRow] = []
    if not refs:
        return rows
    tol = cash_tolerance(p)
    ending = p.reference_cash()                    # summed over the accounts for a consolidated portfolio
    for c in m.cash:
        if c.currency in ending:
            diff = c.balance - ending[c.currency]
            rows.append(CheckRow("Cash balance", c.currency, ending[c.currency], c.balance, diff, tol,
                                 "OK" if abs(diff) <= tol else "CHECK"))
    if p.holdings_source and p.holdings_source != SRC_TRANSACTIONS:
        for pos in m.positions:
            diff = pos.tx_quantity - pos.quantity
            rows.append(CheckRow(SHARE_CHECK, pos.ticker, pos.quantity, pos.tx_quantity, diff, QTY_TOL,
                                 "OK" if abs(diff) <= QTY_TOL else "INFO"))
    nav = p.reference_nav()
    if nav is not None:
        diff = m.base_total - nav
        tol_nav = NAV_REL_TOL * nav
        rows.append(CheckRow(f"Net asset value ({m.base_currency})", m.base_currency, nav, m.base_total, diff, tol_nav,
                             "OK" if abs(diff) <= tol_nav else "INFO"))
    return rows


def overall(rows: list[CheckRow]) -> str:
    bad = sum(1 for r in rows if r.status == "CHECK")
    return "all checks passed" if bad == 0 else f"{bad} check(s) need review"
