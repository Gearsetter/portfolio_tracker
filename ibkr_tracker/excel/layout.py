"""Cell geometry of the workbook - shared by the builder and the in-place refresher.

Main sheet ``Portfolio``::

    A..H   Sections 1-4 stacked vertically, 5 empty rows between sections
    J..P   Section 5 (extension of the Section 1 rows)
    J..Q   Sections 6-9 (+ benchmark block) stacked vertically below Section 5, 5 empty rows after it
    R..T   helper columns (FX rate / benchmark close per transaction; currency, sum of the transactions and share
           factor per position; years)
    I      "Account" of every record in Sections 1-4 - only in the consolidated (all accounts) workbook
"""
from __future__ import annotations

from dataclasses import dataclass

PORTFOLIO = "Portfolio"
PRICES = "Prices"
FXSHEET = "FX"
BENCH = "Benchmark"
CHECKS = "Checks"
ABOUT = "About"

GAP = 5                     # empty rows between stacked sections

# --- Portfolio sheet column letters ---------------------------------------------------------------------
TRADE_COLS = dict(name="A", ticker="B", qty="C", price="D", total="E", fee="F", ccy="G", date="H")
EXT_COLS = dict(gross="J", cur_price="K", cur_total="L", usd_total="M", usd_gross="N", usd_cur="O", vuaa="P",
                fx="R", bench_close="S")
DIV_COLS = dict(name="A", ticker="B", qty="C", per_share="D", total="E", tax="F", ccy="G", date="H")
DEP_COLS = dict(desc="A", amount="B", rate="C", usd="D", date="E", ccy="F", base="G")
FXT_COLS = dict(bought="A", qty="B", price="C", total="D", fee="E", paid="F", date="G", fee_ccy="H")
POS_COLS = dict(ticker="J", qty="K", price="L", value="M", ret="N", usd_value="O", usd_ret="P", cagr="Q", ccy="R",
                tx_qty="S", factor="T")
ACCOUNT_COL = "I"                      # consolidated workbook only: which account a Section 1-4 record belongs to

# --- Prices sheet geometry ------------------------------------------------------------------------------
PX_SETTINGS = dict(base=5, asof=6, days=7, mode=8, refreshed=9, fingerprint=10, bench_symbol=11)
PX_BENCH_ROW = 14                      # benchmark table row (header = 13)
PX_STOCK_HDR = 16                      # stock table header row; data from 17
PX_STOCK_COLS = dict(key="A", ticker="B", ccy="C", symbol="D", last="E", day="F", source="G", override="H", used="I")
PX_FX_COLS = dict(ccy="K", last="L", day="M", source="N", override="O", used="P")
PX_BENCH_COLS = dict(symbol="A", last="B", day="C", source="D", override="E", used="F")

STATE = "_state"                               # hidden sheet: layout version, archive signature, override snapshot
WORKBOOK_VERSION = "ibkr-portfolio-tracker/layout-2"   # bump whenever the cell geometry / formulas change
FILTER_TAG = "+filter"                         # appended to the layout version of a workbook built with the view filter


def layout_tag(hide: bool = False) -> str:
    """What the hidden state sheet stores as ``layout``: the layout version, plus ``+filter`` when Sections 6 / 7 were
    built without the closed positions and dust balances - switching the setting makes the file stale."""
    return WORKBOOK_VERSION + (FILTER_TAG if hide else "")


@dataclass
class Layout:
    n_trades: int
    n_divs: int
    n_deps: int
    n_fx: int
    n_pos: int
    n_cash: int

    # Section 1 / 5
    @property
    def t_hdr(self) -> int: return 1
    @property
    def t_first(self) -> int: return 2
    @property
    def t_last(self) -> int: return 1 + max(self.n_trades, 0)          # header only when empty

    # Section 2
    @property
    def d_hdr(self) -> int: return self.t_last + GAP + 1
    @property
    def d_first(self) -> int: return self.d_hdr + 1
    @property
    def d_last(self) -> int: return self.d_hdr + self.n_divs

    # Section 3
    @property
    def p_hdr(self) -> int: return self.d_last + GAP + 1
    @property
    def p_first(self) -> int: return self.p_hdr + 1
    @property
    def p_last(self) -> int: return self.p_hdr + self.n_deps

    # Section 4
    @property
    def f_hdr(self) -> int: return self.p_last + GAP + 1
    @property
    def f_first(self) -> int: return self.f_hdr + 1
    @property
    def f_last(self) -> int: return self.f_hdr + self.n_fx

    # Section 6 (right block, starts GAP rows below the Section 5 rows)
    @property
    def pos_hdr(self) -> int: return self.t_last + GAP + 1
    @property
    def pos_first(self) -> int: return self.pos_hdr + 1
    @property
    def pos_last(self) -> int: return self.pos_hdr + self.n_pos

    # Section 7 follows Section 6 immediately
    @property
    def cash_first(self) -> int: return self.pos_last + 1
    @property
    def cash_last(self) -> int: return self.pos_last + self.n_cash

    # Section 8 / 9: one empty row after the previous section
    @property
    def sum_usd(self) -> int: return self.cash_last + 2
    @property
    def sum_base(self) -> int: return self.sum_usd + 2

    # Section 10 (benchmark comparison, one empty row after Section 9)
    @property
    def bm_hdr(self) -> int: return self.sum_base + 2

    @staticmethod
    def for_portfolio(p, hide: bool = False) -> "Layout":
        """Geometry of the workbook of ``p``; with ``hide`` Sections 6 / 7 list only the open positions and the
        non-dust cash balances (``Portfolio.visible_positions`` / ``visible_currencies``)."""
        return Layout(len(p.trades), len(p.dividends), len(p.cash_flows), len(p.fx_trades),
                      len(p.visible_positions(hide)), len(p.visible_currencies(hide)))

    @staticmethod
    def rng(first: int, last: int, col: str, sheet: str = PORTFOLIO) -> str:
        """Absolute range; always at least one row so SUMIFS on an empty section stays valid."""
        last = max(first, last)
        return f"{sheet}!${col}${first}:${col}${last}"
