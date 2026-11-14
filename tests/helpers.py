"""Cell-by-cell comparison of a recalculated workbook with the Python oracle (``ibkr_tracker.calc``)."""
from __future__ import annotations

import math
from typing import Any, Optional

from ibkr_tracker.calc import Metrics
from ibkr_tracker.excel import layout as L
from ibkr_tracker.excel.layout import Layout
from ibkr_tracker.portfolio import Portfolio


def layout_for(p: Portfolio) -> Layout:
    return Layout(len(p.trades), len(p.dividends), len(p.cash_flows), len(p.fx_trades), len(p.positions_keys()),
                  len(p.currencies()))


def close(a: Any, b: Any, rel: float = 1e-9, abs_: float = 1e-9) -> bool:
    if a in (None, "") and b in (None, ""):
        return True
    if a is None or b is None or isinstance(a, str) or isinstance(b, str):
        return False
    return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=abs_)


def compare_portfolio_sheet(vals: dict[str, Any], p: Portfolio, m: Metrics,
                            lay: Optional[Layout] = None) -> list[str]:
    """``vals`` = {coordinate: value} of the Portfolio sheet.  Returns a list of mismatch descriptions."""
    lay = lay or layout_for(p)
    bad: list[str] = []

    def chk(ref: str, expected: Any, rel: float = 1e-9, abs_: float = 1e-9) -> None:
        got = vals.get(ref)
        if not close(got, expected, rel, abs_):
            bad.append(f"{ref}: workbook={got!r} python={expected!r}")

    for i, r in enumerate(m.trades):
        row = lay.t_first + i
        t = r.trade
        for col, exp in (("C", t.quantity), ("D", t.price), ("F", t.fee), ("E", r.total), ("J", r.gross),
                         ("K", r.current_price), ("L", r.current_total), ("M", r.usd_total), ("N", r.usd_gross),
                         ("O", r.usd_current), ("R", r.fx_rate), ("P", r.vuaa_qty), ("S", r.bench_close)):
            if col in ("P", "S") and exp is None:
                exp = 0 if col == "S" else None
            chk(f"{col}{row}", exp)
    for i, d in enumerate(p.dividends):
        row = lay.d_first + i
        for col, exp in (("C", d.quantity), ("D", d.per_share), ("E", d.total), ("F", d.tax_fee)):
            chk(f"{col}{row}", exp)
    for i, d in enumerate(m.deposits):
        row = lay.p_first + i
        for col, exp in (("B", d.amount), ("C", d.rate), ("D", d.usd_amount), ("G", d.base_amount)):
            chk(f"{col}{row}", exp)
    for i, f in enumerate(p.fx_trades):
        row = lay.f_first + i
        chk(f"B{row}", f.quantity)
        chk(f"D{row}", f.quantity * f.price)
        chk(f"E{row}", f.fee)
    for i, pos in enumerate(m.positions):
        row = lay.pos_first + i
        if vals.get(f"J{row}") != pos.ticker:
            bad.append(f"J{row}: ticker {vals.get(f'J{row}')!r} != {pos.ticker!r}")
        for col, exp in (("K", pos.quantity), ("L", pos.price), ("M", pos.value), ("N", pos.ret),
                         ("O", pos.usd_value), ("P", pos.usd_ret), ("S", pos.tx_quantity), ("T", pos.share_factor)):
            chk(f"{col}{row}", exp)
    for i, c in enumerate(m.cash):
        row = lay.cash_first + i
        if vals.get(f"J{row}") != c.currency:
            bad.append(f"J{row}: currency {vals.get(f'J{row}')!r} != {c.currency!r}")
        chk(f"K{row}", c.balance, abs_=1e-8)
        chk(f"O{row}", c.usd_value, abs_=1e-8)
    s, h = lay.sum_usd, lay.sum_base
    chk(f"O{s}", m.usd_total)
    chk(f"P{s}", m.usd_return)
    chk(f"R{s}", m.years)
    chk(f"Q{s}", m.usd_cagr)
    chk(f"M{h}", m.base_total)
    chk(f"N{h}", m.base_return)
    chk(f"Q{h}", m.base_cagr)
    b = m.benchmark
    base_row = lay.bm_hdr
    chk(f"K{base_row + 4}", b.invested_usd)
    chk(f"K{base_row + 5}", b.stocks_value_const_fx)
    chk(f"K{base_row + 6}", b.stocks_value_latest_fx)
    if b.available:
        chk(f"K{base_row + 1}", b.units)
        chk(f"K{base_row + 2}", b.price)
        chk(f"K{base_row + 3}", b.value_usd)
        chk(f"K{base_row + 7}", b.ret_vuaa)
        chk(f"K{base_row + 9}", b.excess_usd)
    else:
        if vals.get(f"K{base_row + 7}") != "n/a":
            bad.append(f"K{base_row + 7}: expected 'n/a' without benchmark prices, got {vals.get(f'K{base_row + 7}')!r}")
    for off, exp in ((10, b.years), (11, b.cagr_vuaa), (12, b.cagr_stocks)):        # years, VUAA CAGR, stocks CAGR
        ref = f"K{base_row + off}"
        if exp is None:
            if vals.get(ref) != "n/a":
                bad.append(f"{ref}: expected 'n/a', got {vals.get(ref)!r}")
        else:
            chk(ref, exp)
    return bad


def sheet_values(path, sheet: str) -> dict[str, Any]:
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True)
    return {c.coordinate: c.value for row in wb[sheet].iter_rows() for c in row if c.value is not None}


def error_cells(path) -> list[str]:
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=True)
    errs = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("#") and c.value.rstrip("!?").upper() in (
                        "#REF", "#NAME", "#VALUE", "#DIV/0", "#N/A", "#NUM", "#NULL"):
                    errs.append(f"{ws.title}!{c.coordinate}={c.value}")
    return errs
