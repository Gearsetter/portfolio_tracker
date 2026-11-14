"""Build the formula-driven Excel workbook.

Everything that can be a formula *is* a formula; the only hard-coded numbers are the raw statement records
(blue font) and market data (``Prices`` / ``FX`` / ``Benchmark`` sheets, refreshed in place by
:mod:`ibkr_tracker.excel.refresh`).  Named ranges (``Trade_Qty``, ``Dep_USDAmt`` ...) keep the formulas readable.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time
from typing import Optional

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.hyperlink import Hyperlink
from openpyxl.worksheet.properties import PageSetupProperties
from openpyxl.worksheet.worksheet import Worksheet

from .. import __version__
from ..market.data import MarketData
from ..portfolio import QTY_TOL, SRC_TRANSACTIONS, Portfolio
from ..reconcile import NAV_REL_TOL, SHARE_CHECK, cash_tolerance
from . import layout as L
from . import overrides as OV
from .layout import Layout

FONT = "Arial"
F_NORMAL = Font(name=FONT, size=10)
F_BOLD = Font(name=FONT, size=10, bold=True)
F_INPUT = Font(name=FONT, size=10, color="0000FF")             # hard-coded statement / market data
F_LINK = Font(name=FONT, size=10, color="008000")              # formulas pulling from the other sheets
F_NOTE = Font(name=FONT, size=9, italic=True, color="595959")
F_TITLE = Font(name=FONT, size=14, bold=True)
F_WARN = Font(name=FONT, size=10, bold=True, color="C00000")
FILL_HDR = PatternFill("solid", fgColor="DDEBF7")
FILL_HELP = PatternFill("solid", fgColor="EDEDED")
FILL_SUM = PatternFill("solid", fgColor="F2F2F2")
FILL_IN = PatternFill("solid", fgColor="FFF2CC")               # cells the user may overwrite
_THIN = Side(style="thin", color="595959")
_HAIR = Side(style="thin", color="D0D0D0")
B_HDR = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
B_CELL = Border(left=_HAIR, right=_HAIR, top=_HAIR, bottom=_HAIR)
B_SUM = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)

FMT_PRICE = "#,##0.00####"
FMT_MONEY = "#,##0.00"
FMT_HIDE_ZERO = "#,##0.00####;-#,##0.00####;"
FMT_FX = "0.0000"
FMT_RATIO = '0.0000"x"'
FMT_PCT = "0.00%"
FMT_DATE = "yyyy-mm-dd"
FMT_DT = "yyyy-mm-dd hh:mm:ss"
FMT_YEARS = "0.0000"


class MissingMarketData(RuntimeError):
    """Raised when the workbook cannot be built because a required FX rate is unavailable."""


# ======================================================================================================
# small helpers
# ======================================================================================================
def _put(ws: Worksheet, ref: str, value, fmt: Optional[str] = None, font: Font = F_NORMAL, fill: Optional[PatternFill] = None,
         border: Optional[Border] = B_CELL, align: Optional[str] = None) -> None:
    c = ws[ref]
    c.value = value
    c.font = font
    if fmt:
        c.number_format = fmt
    if fill:
        c.fill = fill
    if border:
        c.border = border
    if align:
        c.alignment = Alignment(horizontal=align, vertical="center")


def _header(ws: Worksheet, row: int, labels: list[tuple[str, str]], helper: Optional[set[str]] = None) -> None:
    for col, text in labels:
        _put(ws, f"{col}{row}", text, font=F_BOLD, fill=FILL_HELP if helper and col in helper else FILL_HDR,
             border=B_HDR, align="center")


def _comment(ws: Worksheet, ref: str, text: str, w: int = 330, h: int = 110) -> None:
    c = Comment(text, "IBKR Portfolio Tracker")
    c.width, c.height = w, h
    ws[ref].comment = c


def _print_setup(ws: Worksheet, landscape: bool = True, title_rows: Optional[str] = None) -> None:
    """A4, one page wide (as many pages tall as needed) - so 'Print' / 'Save as PDF' gives a readable sheet."""
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.page_margins.top = ws.page_margins.bottom = 0.5
    if title_rows:
        ws.print_title_rows = title_rows


def _name(wb: Workbook, name: str, ref: str) -> None:
    wb.defined_names[name] = DefinedName(name, attr_text=ref)


def _dt(d: date | datetime) -> datetime:
    return d if isinstance(d, datetime) else datetime.combine(d, time())


def fx_lookup(date_expr: str, ccy_expr: str) -> str:
    """Units of ``ccy`` per USD on the (as-of) date - ``MATCH(...,1)`` with first-row fallback."""
    return f"INDEX(FX_Table,IFERROR(MATCH(INT({date_expr}),FX_Dates,1),1),MATCH({ccy_expr},FX_Hdr,0))"


def fx_latest(ccy_expr: str) -> str:
    return f"INDEX(FXL_Used,MATCH({ccy_expr},FXL_Ccy,0))"


# ======================================================================================================
# main entry point
# ======================================================================================================
def build_workbook(portfolio: Portfolio, md: MarketData, settings: dict, generated_at: Optional[datetime] = None) -> Workbook:
    if md.missing_fx:
        raise MissingMarketData(
            "No FX rate available for " + ", ".join(md.missing_fx) +
            ". Press 'Refresh Prices' (online) or enter a manual rate in the Market data tab.")

    base = portfolio.base_currency or "HUF"
    generated_at = generated_at or datetime.now()
    positions = portfolio.positions_keys()
    currencies = portfolio.currencies()
    lay = Layout(len(portfolio.trades), len(portfolio.dividends), len(portfolio.cash_flows),
                 len(portfolio.fx_trades), len(positions), len(currencies))

    wb = Workbook()
    ws = wb.active
    ws.title = L.PORTFOLIO
    wsp = wb.create_sheet(L.PRICES)
    wsf = wb.create_sheet(L.FXSHEET)
    wsb = wb.create_sheet(L.BENCH)
    wsc = wb.create_sheet(L.CHECKS)
    wsa = wb.create_sheet(L.ABOUT)
    for sheet in wb.worksheets:
        sheet.sheet_view.showGridLines = False

    fx_cols = _fx_columns(md, base)
    _write_prices(wsp, portfolio, md, settings, lay, base, positions, currencies, fx_cols, generated_at)
    _write_fx(wsf, md, fx_cols, wb)
    _write_bench(wsb, md, wb)
    _write_portfolio(ws, wb, portfolio, md, lay, base, positions, currencies)
    _write_checks(wsc, portfolio, lay, positions, currencies, base)
    _write_about(wsa, portfolio, md, lay, base, generated_at)
    _define_names(wb, lay, positions, currencies, fx_cols)
    write_state(wb, portfolio, md, settings)

    wb.calculation.fullCalcOnLoad = True
    wb.properties.title = f"IBKR portfolio {portfolio.label}"
    wb.properties.creator = f"IBKR Portfolio Tracker {__version__}"
    wb.active = 0
    return wb


def _fx_columns(md: MarketData, base: str) -> list[str]:
    cols = ["USD"]
    if base != "USD" and md.fx.has(base):
        cols.append(base)
    cols += [c for c in md.fx.currencies() if c not in cols]
    return cols


# ======================================================================================================
# Portfolio sheet
# ======================================================================================================
def _write_portfolio(ws: Worksheet, wb: Workbook, p: Portfolio, md: MarketData, lay: Layout, base: str,
                     positions: list[tuple[str, str]], currencies: list[str]) -> None:
    widths = dict(A=38, B=11, C=14, D=14, E=16, F=15, G=20, H=20, I=3, J=17, K=18, L=15, M=17, N=13, O=17,
                  P=15, Q=12, R=17, S=17, T=15)
    if p.is_consolidated:
        widths[L.ACCOUNT_COL] = 13
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    T, E, D, P_, F_, S = L.TRADE_COLS, L.EXT_COLS, L.DIV_COLS, L.DEP_COLS, L.FXT_COLS, L.POS_COLS
    ac = L.ACCOUNT_COL

    def acct_header(row: int) -> None:                  # consolidated workbook: which account a record belongs to
        if p.is_consolidated:
            _put(ws, f"{ac}{row}", "Account", font=F_BOLD, fill=FILL_HELP, border=B_HDR, align="center")

    def acct_cell(row: int, value: str) -> None:
        if p.is_consolidated:
            _put(ws, f"{ac}{row}", value, font=F_INPUT, fill=FILL_HELP, align="center")

    # ---- Section 1: stock transactions + Section 5 extensions ----------------------------------------------
    _header(ws, lay.t_hdr, [("A", "Name"), ("B", "Ticker"), ("C", "Quantity"), ("D", "Price"), ("E", "Total"),
                            ("F", "Fee"), ("G", "Currency"), ("H", "Date")])
    _header(ws, lay.t_hdr, [("J", "Gross"), ("K", "Current Price"), ("L", "Current Total"), ("M", "USD Total"),
                            ("N", "USD Gross"), ("O", "USD Current Total"), ("P", "VUAA Quantity"),
                            ("R", "FX rate (per USD)"), ("S", "VUAA close (USD)")], helper={"R", "S"})
    acct_header(lay.t_hdr)
    _comment(ws, f"A{lay.t_hdr}", "Section 1 - Stock transactions (chronological). Sells carry negative quantity "
             "and total. Fee is a POSITIVE cost (IBKR reports commissions as negative numbers).")
    _comment(ws, f"E{lay.t_hdr}", "Total = Quantity x Price (negative for sells).", 260, 50)
    _comment(ws, f"J{lay.t_hdr}", "Section 5 - extensions of the Section 1 rows.\nGross = Total + Fee (cash paid incl. "
             "commission; for sells the negative of the net proceeds).", 300, 80)
    _comment(ws, f"L{lay.t_hdr}", "Current Total = Quantity x Current Price x share factor (Section 6, column T). "
             "The factor is 1 unless a stock split / transfer makes the statement's share count differ from the sum "
             "of the transactions; it expresses the transaction in today's shares.", 340, 100)
    _comment(ws, f"M{lay.t_hdr}", "Total converted to USD with the closing USD rate on the transaction date "
             "(as-of lookup in the FX sheet; unchanged for USD). Column O uses the SAME purchase-date rate by "
             "design, so it isolates the price performance from currency moves.", 340, 110)
    _comment(ws, f"P{lay.t_hdr}", "Units of the benchmark ETF (default VUAA) that the same USD Total would have "
             "bought at that day's closing price. Needs benchmark prices (press Refresh Prices).", 320, 90)
    for i, t in enumerate(p.trades):
        r = lay.t_first + i
        _put(ws, f"A{r}", t.name, font=F_INPUT)
        _put(ws, f"B{r}", t.ticker, font=F_INPUT)
        _put(ws, f"C{r}", t.quantity, "General", F_INPUT)
        _put(ws, f"D{r}", t.price, FMT_PRICE, F_INPUT)
        _put(ws, f"E{r}", f"=C{r}*D{r}", FMT_MONEY)
        _put(ws, f"F{r}", t.fee, FMT_PRICE, F_INPUT)
        _put(ws, f"G{r}", t.currency, font=F_INPUT, align="center")
        _put(ws, f"H{r}", t.dt, FMT_DT, F_INPUT)
        _put(ws, f"J{r}", f"=E{r}+F{r}", FMT_MONEY)
        _put(ws, f"K{r}", f'=INDEX(Px_Used,MATCH(B{r}&"|"&G{r},Px_Key,0))', FMT_PRICE, F_LINK)
        _put(ws, f"L{r}", f"=C{r}*K{r}*SUMIFS(Pos_Factor,Pos_Ticker,B{r},Pos_Ccy,G{r})", FMT_MONEY)
        acct_cell(r, t.account)
        _put(ws, f"M{r}", f"=E{r}/R{r}", FMT_MONEY)
        _put(ws, f"N{r}", f"=J{r}/R{r}", FMT_MONEY)
        _put(ws, f"O{r}", f"=L{r}/R{r}", FMT_MONEY)
        _put(ws, f"P{r}", f'=IF(N(S{r})>0,M{r}/S{r},"")', "0.0000", F_NORMAL)
        _put(ws, f"R{r}", "=" + fx_lookup(f"H{r}", f"G{r}"), FMT_FX, F_LINK, fill=FILL_HELP)
        _put(ws, f"S{r}", "=IFERROR(INDEX(BM_USD,IFERROR(MATCH(INT(H" + str(r) + "),BM_Dates,1),1)),0)",
             FMT_HIDE_ZERO, F_LINK, fill=FILL_HELP)                       # 0 = no benchmark price yet -> shown blank

    # ---- Section 2: dividends -------------------------------------------------------------------------------
    _header(ws, lay.d_hdr, [("A", "Name"), ("B", "Ticker"), ("C", "Quantity"), ("D", "Dividend"), ("E", "Total"),
                            ("F", "Withholding Tax"), ("G", "Currency"), ("H", "Date")])
    acct_header(lay.d_hdr)
    _comment(ws, f"A{lay.d_hdr}", "Section 2 - Dividend payouts (chronological). Dividend, withholding tax and fees "
             "of the same payment are synchronised on one row.", 320, 80)
    _comment(ws, f"D{lay.d_hdr}", "Dividend per share (from the IBKR description). Quantity = shares that earned it "
             "(IBKR dividend-accrual table).", 300, 70)
    _comment(ws, f"E{lay.d_hdr}", "Gross amount credited by IBKR (rounded to cents by IBKR, so it can differ "
             "slightly from Quantity x Dividend).", 300, 70)
    _comment(ws, f"F{lay.d_hdr}", "Withholding tax PLUS any dividend-related fees (e.g. ADR fee) - a positive cost.",
             300, 60)
    for i, d in enumerate(p.dividends):
        r = lay.d_first + i
        _put(ws, f"A{r}", d.name, font=F_INPUT)
        _put(ws, f"B{r}", d.ticker, font=F_INPUT)
        _put(ws, f"C{r}", d.quantity, "General", F_INPUT)
        _put(ws, f"D{r}", d.per_share, FMT_PRICE, F_INPUT)
        _put(ws, f"E{r}", d.total, FMT_MONEY, F_INPUT)
        _put(ws, f"F{r}", d.tax_fee, FMT_MONEY, F_INPUT)
        _put(ws, f"G{r}", d.currency, font=F_INPUT, align="center")
        _put(ws, f"H{r}", _dt(d.dt), FMT_DATE, F_INPUT)
        acct_cell(r, d.account)

    # ---- Section 3: deposits & withdrawals -----------------------------------------------------------------
    _header(ws, lay.p_hdr, [("A", "Description"), ("B", "Amount"), ("C", f"USD.{base}"), ("D", "USD Amount"),
                            ("E", "Date"), ("F", "Currency"), ("G", f"Amount ({base})")], helper={"F", "G"})
    acct_header(lay.p_hdr)
    _comment(ws, f"A{lay.p_hdr}", "Section 3 - Deposits, withdrawals, monthly fees (and interest). Hover over a "
             "description cell for the original IBKR text. Columns F-G are helper columns.", 330, 90)
    _comment(ws, f"C{lay.p_hdr}", f"Closing {base} per 1 USD on that date (quote 'USD.{base}', e.g. ~320 for HUF).",
             280, 60)
    _comment(ws, f"D{lay.p_hdr}", f"Amount converted to USD. Because USD.{base} is quoted as {base} per USD, "
             f"the conversion DIVIDES by it (Amount / USD.{base}); multiplying a {base} amount by ~320 "
             f"would be meaningless.", 340, 100)
    for i, c in enumerate(p.cash_flows):
        r = lay.p_first + i
        _put(ws, f"A{r}", c.kind, font=F_INPUT)
        if c.description:
            _comment(ws, f"A{r}", c.description, 320, 60)
        _put(ws, f"B{r}", c.amount, FMT_MONEY, F_INPUT)
        _put(ws, f"C{r}", f'=IF(BaseCcy="USD",1,{fx_lookup(f"E{r}", "BaseCcy")})', FMT_FX, F_LINK)
        _put(ws, f"D{r}", f'=IF(F{r}="USD",B{r},IF(F{r}=BaseCcy,B{r}/C{r},B{r}/{fx_lookup(f"E{r}", f"F{r}")}))',
             FMT_MONEY, F_LINK)
        _put(ws, f"E{r}", _dt(c.dt), FMT_DATE, F_INPUT)
        _put(ws, f"F{r}", c.currency, font=F_INPUT, fill=FILL_HELP, align="center")
        _put(ws, f"G{r}", f"=IF(F{r}=BaseCcy,B{r},D{r}*C{r})", FMT_MONEY, F_NORMAL, fill=FILL_HELP)
        acct_cell(r, c.account)

    # ---- Section 4: forex ---------------------------------------------------------------------------------
    _header(ws, lay.f_hdr, [("A", "Currency Bought"), ("B", "Quantity"), ("C", "Price"), ("D", "Total"),
                            ("E", "Fee"), ("F", "Currency Paid"), ("G", "Date"), ("H", "Fee Currency")],
            helper={"H"})
    acct_header(lay.f_hdr)
    _comment(ws, f"A{lay.f_hdr}", "Section 4 - Currency transactions, e.g. USD.HUF: USD (Currency Bought) is bought "
             "with HUF (Currency Paid). Negative quantity = the currency was sold (end-of-day sweeps). Fee is a "
             "positive cost in the 'Fee Currency' (IBKR charges forex commission in the base currency).", 360, 120)
    for i, f in enumerate(p.fx_trades):
        r = lay.f_first + i
        _put(ws, f"A{r}", f.bought, font=F_INPUT, align="center")
        _put(ws, f"B{r}", f.quantity, "General", F_INPUT)
        _put(ws, f"C{r}", f.price, "0.0000####", F_INPUT)
        _put(ws, f"D{r}", f"=B{r}*C{r}", FMT_MONEY)
        _put(ws, f"E{r}", f.fee, FMT_MONEY, F_INPUT)
        _put(ws, f"F{r}", f.paid, font=F_INPUT, align="center")
        _put(ws, f"G{r}", f.dt, FMT_DT, F_INPUT)
        _put(ws, f"H{r}", f.fee_currency, font=F_INPUT, fill=FILL_HELP, align="center")
        acct_cell(r, f.account)

    # ---- Section 6: positions ------------------------------------------------------------------------------
    _header(ws, lay.pos_hdr, [("J", "Ticker"), ("K", "Quantity"), ("L", "Current Price"), ("M", "Value"),
                              ("N", "Return"), ("O", "USD Value"), ("P", "USD Return"), ("Q", "CAGR"),
                              ("R", "Currency"), ("S", "Transactions (sum)"), ("T", "Share factor")],
            helper={"R", "S", "T"})
    _comment(ws, f"J{lay.pos_hdr}", "Section 6 - Portfolio positions (one row per ticker ever bought or held). "
             "Value is in the stock's own currency; USD Value uses the latest USD rate.", 330, 80)
    _comment(ws, f"K{lay.pos_hdr}", "Shares held = the 'Open Positions' section of the latest Activity Statement "
             "(blue = taken from it). It is the source of truth, so stock splits and other corporate actions are "
             "reflected; the corporate-action section and the sum of the transactions are not used. "
             f"[{p.holdings_source or 'transactions'}: {p.holdings_ref or 'n/a'}]", 360, 140)
    _comment(ws, f"S{lay.pos_hdr}", "Sum of the signed quantities of the Section 1 transactions - for comparison "
             "only (see the Checks sheet).", 280, 60)
    _comment(ws, f"T{lay.pos_hdr}", "Statement shares / sum of the transactions (1 when they agree, e.g. 10 after a "
             "10:1 split). Section 5 'Current Total' multiplies each transaction by it.", 300, 80)
    _comment(ws, f"N{lay.pos_hdr}", "Return = Value / Gross (value as a multiple of the cash invested incl. fees; "
             "1.10x = +10%). Blank for closed positions.", 300, 70)
    _comment(ws, f"Q{lay.pos_hdr}", "Left empty by design (CAGR is reported for the whole portfolio in "
             "Sections 8 and 9).", 260, 50)
    held = p.net_quantities()
    from_statement = p.holdings_source not in ("", SRC_TRANSACTIONS)
    for i, (ticker, ccy) in enumerate(positions):
        r = lay.pos_first + i
        crit = f"Trade_Ticker,$J{r},Trade_Ccy,$R{r}"
        _put(ws, f"J{r}", ticker, font=F_INPUT)
        if from_statement:
            _put(ws, f"K{r}", held.get((ticker, ccy), 0.0), "General", F_INPUT)
        else:                                                  # no Open Positions anywhere: sum of the transactions
            _put(ws, f"K{r}", f"=SUMIFS(Trade_Qty,{crit})", "General")
        _put(ws, f"L{r}", f'=INDEX(Px_Used,MATCH($J{r}&"|"&$R{r},Px_Key,0))', FMT_PRICE, F_LINK)
        _put(ws, f"M{r}", f"=K{r}*L{r}", FMT_MONEY)
        _put(ws, f"N{r}", f'=IF(K{r}=0,"",IFERROR(M{r}/SUMIFS(Trade_Gross,{crit}),""))', FMT_RATIO)
        _put(ws, f"O{r}", f"=M{r}/{fx_latest(f'$R{r}')}", FMT_MONEY, F_LINK)
        _put(ws, f"P{r}", f'=IF(K{r}=0,"",IFERROR(O{r}/SUMIFS(Trade_USDGross,{crit}),""))', FMT_RATIO)
        _put(ws, f"Q{r}", None)
        _put(ws, f"R{r}", ccy, font=F_INPUT, fill=FILL_HELP, align="center")
        _put(ws, f"S{r}", f"=SUMIFS(Trade_Qty,{crit})", "General", F_NORMAL, FILL_HELP)
        _put(ws, f"T{r}", f"=IF(ABS(S{r})<{QTY_TOL:.6f},1,K{r}/S{r})", "0.0000", F_NORMAL, FILL_HELP)

    # ---- Section 7: cash balances --------------------------------------------------------------------------
    active = set(p.active_currencies())
    for i, ccy in enumerate(currencies):
        r = lay.cash_first + i
        _put(ws, f"J{r}", ccy, font=F_INPUT)
        cash = ("=SUMIFS(Dep_Amount,Dep_Ccy,$J{r})"
                "+SUMIFS(Div_Total,Div_Ccy,$J{r})-SUMIFS(Div_Tax,Div_Ccy,$J{r})"
                "-SUMIFS(Trade_Gross,Trade_Ccy,$J{r})"
                "+SUMIFS(FXT_Qty,FXT_Bought,$J{r})-SUMIFS(FXT_Total,FXT_Paid,$J{r})"
                "-SUMIFS(FXT_Fee,FXT_FeeCcy,$J{r})").format(r=r)
        _put(ws, f"K{r}", cash, FMT_MONEY)
        usd = f"K{r}/{fx_latest(f'$J{r}')}"
        _put(ws, f"O{r}", f"={usd}" if ccy in active or md.fx.has(ccy) else f"=IFERROR({usd},0)", FMT_MONEY, F_LINK)
        for col in "LMNPQ":
            _put(ws, f"{col}{r}", None)
    _comment(ws, f"K{lay.cash_first}", "Section 7 - Net cash per currency = deposits/withdrawals/fees + dividends - "
             "withholding tax/fees - stock trades (Gross) + forex bought - forex paid - forex fees. Ties to the "
             "'Ending Cash' lines of the IBKR Cash Report (see the Checks sheet).", 360, 110)

    # ---- Section 8: USD summary ----------------------------------------------------------------------------
    s = lay.sum_usd
    for col in "JKLMNOPQ":
        _put(ws, f"{col}{s}", None, font=F_BOLD, fill=FILL_SUM, border=B_SUM)
    _put(ws, f"J{s}", "Summary (USD)", font=F_BOLD, fill=FILL_SUM, border=B_SUM)
    _put(ws, f"O{s}", f"=SUM(O{lay.pos_first}:O{lay.cash_last})", FMT_MONEY + ' "USD"', F_BOLD, FILL_SUM, B_SUM)
    _put(ws, f"P{s}", f'=IFERROR(O{s}/SUMIFS(Dep_USDAmt,Dep_Desc,"<>interest"),"")', FMT_RATIO, F_BOLD, FILL_SUM, B_SUM)
    _put(ws, f"Q{s}", f'=IFERROR(P{s}^(1/R{s})-1,"")', FMT_PCT, F_BOLD, FILL_SUM, B_SUM)
    _put(ws, f"R{s}", '=IFERROR(SUMPRODUCT(Dep_USDAmt*(Dep_Desc<>"interest")*(AsOfDate-INT(Dep_Date)))'
                      '/SUMIFS(Dep_USDAmt,Dep_Desc,"<>interest")/DaysPerYear,"")', FMT_YEARS, F_NORMAL, FILL_HELP, B_CELL)
    _put(ws, f"S{s}", "<- weighted avg. years since deposits", font=F_NOTE, border=None)
    _comment(ws, f"O{s}", "Section 8 - Sum of the USD Value cells of Sections 6 and 7 (stocks + cash).", 280, 50)
    _comment(ws, f"P{s}", "Total USD value / sum of the USD Amount cells of Section 3 (deposits net of withdrawals "
             "and monthly fees; interest rows are not contributions).", 320, 80)
    _comment(ws, f"Q{s}", "Time-weighted CAGR = Return^(1 / weighted average years since deposits) - 1, weights = "
             "USD deposit amounts, years measured to the valuation date (Prices!B6) with Prices!B7 days per year. "
             "The '-1' turns the growth factor into a rate (standard CAGR definition).", 360, 120)

    # ---- Section 9: base currency summary ---------------------------------------------------------------------
    h = lay.sum_base
    for col in "JKLMNOPQ":
        _put(ws, f"{col}{h}", None, font=F_BOLD, fill=FILL_SUM, border=B_SUM)
    _put(ws, f"J{h}", f"Summary ({base})", font=F_BOLD, fill=FILL_SUM, border=B_SUM)
    _put(ws, f"M{h}", f"=O{s}*{fx_latest('BaseCcy')}", f'#,##0.00 "{base}"', F_BOLD, FILL_SUM, B_SUM)
    _put(ws, f"N{h}", f'=IFERROR(M{h}/SUMIFS(Dep_BaseAmt,Dep_Desc,"<>interest"),"")', FMT_RATIO, F_BOLD, FILL_SUM, B_SUM)
    _put(ws, f"Q{h}", f'=IFERROR(N{h}^(1/R{s})-1,"")', FMT_PCT, F_BOLD, FILL_SUM, B_SUM)
    _put(ws, f"R{h}", f"=R{s}", FMT_YEARS, F_NORMAL, FILL_HELP, B_CELL)
    _comment(ws, f"M{h}", f"Summary (USD) converted to {base} with the latest USD.{base} rate.", 280, 50)
    _comment(ws, f"N{h}", f"{base} value / sum of the {base} Amount cells of Section 3.", 280, 50)
    _comment(ws, f"Q{h}", "Time-weighted CAGR in base currency: Return^(1 / weighted average years) - 1, "
             "weighted by the deposits' USD amounts.", 320, 70)

    # ---- Section 10: benchmark comparison -----------------------------------------------------------------
    b = lay.bm_hdr
    _header(ws, b, [("J", "Benchmark (VUAA)"), ("K", "Value")])
    _comment(ws, f"J{b}", "Extra block (not in the original spec): what the same USD amounts would be worth if each "
             "stock purchase had been put into the benchmark ETF instead (Section 5, column P).", 340, 90)
    rows = [
        ("VUAA units (sum of column P)", "=SUM(Trade_VUAAQty)", "0.0000"),
        ("VUAA price (USD, latest)", "=BenchPriceUSD", FMT_PRICE),
        ("VUAA value (USD)", f"=K{b + 1}*K{b + 2}", FMT_MONEY),
        ("Invested in stocks (USD)", "=SUM(Trade_USDTotal)", FMT_MONEY),
        ("Stocks now - purchase-date FX (USD)", "=SUM(Trade_USDCur)", FMT_MONEY),
        ("Stocks now - latest FX (USD)", f"=SUM(O{lay.pos_first}:O{max(lay.pos_first, lay.pos_last)})", FMT_MONEY),
        ("VUAA return", f'=IF(K{b + 1}>0,K{b + 3}/K{b + 4},"n/a")', FMT_RATIO),
        ("Stocks return (latest FX)", f'=IFERROR(K{b + 6}/K{b + 4},"n/a")', FMT_RATIO),
        ("Stocks minus VUAA (USD)", f'=IF(K{b + 1}>0,K{b + 6}-K{b + 3},"n/a")', FMT_MONEY),
        ("Avg. years since the purchases",
         f'=IFERROR(SUMPRODUCT(Trade_USDTotal*(AsOfDate-INT(Trade_Date)))/K{b + 4}/DaysPerYear,"n/a")', FMT_YEARS),
        ("VUAA CAGR (USD)", f'=IFERROR(IF(K{b + 10}>0,K{b + 7}^(1/K{b + 10})-1,"n/a"),"n/a")', FMT_PCT),
        ("Stocks CAGR (USD, same basis)", f'=IFERROR(IF(K{b + 10}>0,K{b + 8}^(1/K{b + 10})-1,"n/a"),"n/a")', FMT_PCT),
    ]
    for i, (label, formula, fmt) in enumerate(rows, start=1):
        _put(ws, f"J{b + i}", label)
        _put(ws, f"K{b + i}", formula, fmt, F_LINK if "Bench" in formula else F_NORMAL)
    _comment(ws, f"J{b + 10}", "Weighted average time from each stock transaction (weight = its USD Total, sells "
             "negative) to the valuation date, in years (Prices!B7 days per year).", 320, 80)
    _comment(ws, f"J{b + 11}", "Time-weighted CAGR of the VUAA simulation in USD: (VUAA value / invested)^(1 / avg. "
             "years since the purchases) - 1. Every stock transaction is replayed into the benchmark ETF at that "
             "day's close (Section 5, column P). 'Stocks CAGR' below uses the same years and invested amount, so "
             "the two lines are directly comparable.", 360, 130)
    ws.column_dimensions["J"].width = 36
    _print_setup(ws)


# ======================================================================================================
# Prices sheet
# ======================================================================================================
def _write_prices(ws: Worksheet, p: Portfolio, md: MarketData, settings: dict, lay: Layout, base: str,
                  positions: list[tuple[str, str]], currencies: list[str], fx_cols: list[str],
                  generated_at: datetime) -> None:
    for col, w in dict(A=26, B=18, C=14, D=22, E=16, F=14, G=36, H=14, I=16, J=3, K=12, L=16, M=14, N=34, O=14,
                       P=16).items():
        ws.column_dimensions[col].width = w
    _put(ws, "A1", "Market data & settings", font=F_TITLE, border=None)
    _put(ws, "A2", "Blue = data written by the app (Refresh Prices / statements). Yellow cells are yours: a value in an "
         "'Override' column wins over the downloaded number and survives Refresh Prices. All sheets read from here.", font=F_NOTE, border=None)
    _put(ws, "A4", "Settings", font=F_BOLD, border=None)
    S = L.PX_SETTINGS
    rows = [
        (S["base"], "Base currency", base, None, F_INPUT),
        (S["asof"], "Valuation date (as-of)", _dt(md.as_of), FMT_DATE, F_INPUT),
        (S["days"], "Days per year (CAGR)", float(settings.get("days_per_year", 365)), "0", F_INPUT),
        (S["mode"], "Data mode", md.mode, None, F_NORMAL),
        (S["refreshed"], "Last price refresh", md.last_refresh or "never (statement data only)", None, F_NORMAL),
        (S["fingerprint"], "Archive signature", p.signature(), None, F_NORMAL),
        (S["bench_symbol"], "Benchmark Yahoo symbol (set in the app)", settings.get("benchmark_symbol", "VUAA.L"), None,
         F_INPUT),
    ]
    for r, label, value, fmt, font in rows:
        _put(ws, f"A{r}", label, border=B_CELL)
        _put(ws, f"B{r}", value, fmt, font, fill=FILL_IN if r in (S["days"],) else None, align="left")
    _comment(ws, f"B{S['asof']}", "Date of the latest price used for the holdings. 'Elapsed time since deposits' "
             "in the CAGR is measured up to this date (not TODAY(), so numbers do not drift between refreshes).", 330, 90)
    _put(ws, "A12", "Benchmark (S&P 500 ETF)", font=F_BOLD, border=None)
    _header(ws, 13, [("A", "Symbol"), ("B", "Last close (USD)"), ("C", "Price date"), ("D", "Source"),
                     ("E", "Override"), ("F", "Price used (USD)")])
    bm = md.benchmark.latest
    r = L.PX_BENCH_ROW
    _put(ws, f"A{r}", settings.get("benchmark_symbol", "VUAA.L"), font=F_INPUT)
    _put(ws, f"B{r}", bm.price if bm else None, FMT_PRICE, F_INPUT)
    _put(ws, f"C{r}", _dt(bm.day) if bm else None, FMT_DATE, F_INPUT)
    _put(ws, f"D{r}", bm.source if bm else "no data yet - press Refresh Prices", font=F_INPUT)
    _put(ws, f"E{r}", md.benchmark_override, FMT_PRICE, F_INPUT, FILL_IN)
    _put(ws, f"F{r}", f'=IF(E{r}<>"",E{r},B{r})', FMT_PRICE)

    # stock prices
    _put(ws, f"A{L.PX_STOCK_HDR - 1}", "Stock prices (latest close)", font=F_BOLD, border=None)
    _header(ws, L.PX_STOCK_HDR, [("A", "Key"), ("B", "Ticker"), ("C", "Currency"), ("D", "Yahoo symbol"),
                                 ("E", "Last close"), ("F", "Price date"), ("G", "Source"), ("H", "Override"),
                                 ("I", "Price used")], helper={"A"})
    for i, (ticker, ccy) in enumerate(positions):
        r = L.PX_STOCK_HDR + 1 + i
        key = f"{ticker}|{ccy}"
        pt = md.raw_prices.get(key)
        _put(ws, f"A{r}", f'=B{r}&"|"&C{r}', fill=FILL_HELP)
        _put(ws, f"B{r}", ticker, font=F_INPUT)
        _put(ws, f"C{r}", ccy, font=F_INPUT, align="center")
        _put(ws, f"D{r}", md.yahoo_symbols.get(key, ticker), font=F_INPUT)
        _put(ws, f"E{r}", pt.price if pt else None, FMT_PRICE, F_INPUT)
        _put(ws, f"F{r}", _dt(pt.day) if pt else None, FMT_DATE, F_INPUT)
        _put(ws, f"G{r}", pt.source if pt else "MISSING - refresh or enter an override",
             font=F_INPUT if pt else F_WARN)
        _put(ws, f"H{r}", md.price_overrides.get(key), FMT_PRICE, F_INPUT, FILL_IN)
        _put(ws, f"I{r}", f'=IF(H{r}<>"",H{r},E{r})', FMT_PRICE)

    # latest FX
    _put(ws, f"K{L.PX_STOCK_HDR - 1}", "Latest FX (units per 1 USD)", font=F_BOLD, border=None)
    _header(ws, L.PX_STOCK_HDR, [("K", "Currency"), ("L", "Last rate"), ("M", "Rate date"), ("N", "Source"),
                                 ("O", "Override"), ("P", "Rate used")])
    fx_row_list = OV.fx_rows(currencies, fx_cols)
    for i, ccy in enumerate(fx_row_list):
        r = L.PX_STOCK_HDR + 1 + i
        pt = md.fx_latest_raw.get(ccy)
        _put(ws, f"K{r}", ccy, font=F_INPUT, align="center")
        if ccy == "USD":
            _put(ws, f"L{r}", 1.0, FMT_FX, F_INPUT)
            _put(ws, f"M{r}", None)
            _put(ws, f"N{r}", "base of the quote", font=F_INPUT)
            _put(ws, f"O{r}", None)
        else:
            _put(ws, f"L{r}", pt.price if pt else None, "0.0000####", F_INPUT)
            _put(ws, f"M{r}", _dt(pt.day) if pt else None, FMT_DATE, F_INPUT)
            _put(ws, f"N{r}", pt.source if pt else "no rate (no activity in this currency)", font=F_INPUT)
            _put(ws, f"O{r}", md.fx_overrides.get(ccy), "0.0000####", F_INPUT, FILL_IN)
        _put(ws, f"P{r}", f'=IF(O{r}<>"",O{r},L{r})', "0.0000####")
    _put(ws, f"K{L.PX_STOCK_HDR + len(fx_row_list) + 3}", f"Quote: units of the currency per 1 USD "
         f"(USD.{base} = {base} per USD). Convert to USD by dividing.", font=F_NOTE, border=None)
    ws.sheet_properties.tabColor = "2E75B6"
    _print_setup(ws)


# ======================================================================================================
# FX and Benchmark history
# ======================================================================================================
def _write_fx(ws: Worksheet, md: MarketData, fx_cols: list[str], wb: Workbook) -> None:
    ws.column_dimensions["A"].width = 13
    _put(ws, "A1", "Date", font=F_BOLD, fill=FILL_HDR, border=B_HDR, align="center")
    _comment(ws, "A1", "Daily closing FX: units of each currency per 1 USD (market quote USD.XXX). "
             "Rewritten by Refresh Prices. Lookups use the last date on or before the transaction date.", 320, 80)
    for j, ccy in enumerate(fx_cols):
        col = get_column_letter(2 + j)
        ws.column_dimensions[col].width = 12
        _put(ws, f"{col}1", ccy, font=F_BOLD, fill=FILL_HDR, border=B_HDR, align="center")
    rows = md.fx.matrix([c for c in fx_cols if c != "USD"])
    if not rows:                                          # USD-only account: keep the ranges valid
        rows = [(md.as_of, [])]
    for i, (d, vals) in enumerate(rows):
        r = 2 + i
        _put(ws, f"A{r}", _dt(d), FMT_DATE, F_INPUT, border=None)
        _put(ws, f"B{r}", 1.0, FMT_FX, F_INPUT, border=None)
        for j, v in enumerate(vals):
            _put(ws, f"{get_column_letter(3 + j)}{r}", v, "0.0000####", F_INPUT, border=None)
    ws.freeze_panes = "B2"
    ws.sheet_properties.tabColor = "2E75B6"
    _print_setup(ws, landscape=False, title_rows="1:1")


def _write_bench(ws: Worksheet, md: MarketData, wb: Workbook) -> None:
    for col, w in dict(A=13, B=16, C=18, D=30).items():
        ws.column_dimensions[col].width = w
    _put(ws, "A1", "Date", font=F_BOLD, fill=FILL_HDR, border=B_HDR, align="center")
    _put(ws, "B1", "Close (USD)", font=F_BOLD, fill=FILL_HDR, border=B_HDR, align="center")
    _put(ws, "C1", "Close (quote ccy)", font=F_BOLD, fill=FILL_HDR, border=B_HDR, align="center")
    _put(ws, "D1", f"{md.benchmark.symbol or 'benchmark'} ({md.benchmark.currency}) - {md.benchmark.source or 'no data'}",
         font=F_NOTE, border=None)
    for i, (d, usd, nat) in enumerate(zip(md.benchmark.dates, md.benchmark.usd, md.benchmark.native)):
        r = 2 + i
        _put(ws, f"A{r}", _dt(d), FMT_DATE, F_INPUT, border=None)
        _put(ws, f"B{r}", usd, "#,##0.00####", F_INPUT, border=None)
        _put(ws, f"C{r}", nat, "#,##0.00####", F_INPUT, border=None)
    ws.freeze_panes = "A2"
    ws.sheet_properties.tabColor = "2E75B6"
    _print_setup(ws, landscape=False, title_rows="1:1")


# ======================================================================================================
# Checks and About
# ======================================================================================================
def _write_checks(ws: Worksheet, p: Portfolio, lay: Layout, positions: list[tuple[str, str]],
                  currencies: list[str], base: str) -> None:
    for col, w in dict(A=24, B=14, C=20, D=20, E=16, F=14, G=12).items():
        ws.column_dimensions[col].width = w
    refs = p.latest_statements()
    _put(ws, "A1", "Reconciliation with the IBKR statement", font=F_TITLE, border=None)
    src = "; ".join(f"{s.filename} (period end {s.period_end})" for s in refs) or "n/a"
    _put(ws, "A2", f"Reference: Cash Report / NAV of {src}. Differences mean missing statements, unsupported "
         f"transaction types, or (NAV) prices that differ from the statement date. The share counts are TAKEN from "
         f"the Open Positions section; the 'Share count' rows only show where the transaction sum differs (INFO: "
         f"stock split, transfer, earlier history).", font=F_NOTE, border=None)
    _put(ws, "A3", "Cash tolerance (abs.)", border=B_CELL)
    _put(ws, "B3", cash_tolerance(p), "0.0000", F_INPUT, FILL_IN)
    _comment(ws, "B3", "0.01 for full-precision (CSV) data; +0.0051 per amount that IBKR's HTML rounded to cents "
             "(worst-case bound).", 300, 60)
    _put(ws, "D3", "NAV tolerance (relative)", border=B_CELL)
    _put(ws, "E3", NAV_REL_TOL, "0.00%", F_INPUT, FILL_IN)
    _header(ws, 5, [("A", "Check"), ("B", "Item"), ("C", "IBKR statement"), ("D", "Workbook"),
                    ("E", "Difference"), ("F", "Tolerance"), ("G", "Status")])
    r = 6
    first = r
    ending = p.reference_cash()
    for i, ccy in enumerate(currencies):
        if ccy in ending:
            _put(ws, f"A{r}", "Cash balance")
            _put(ws, f"B{r}", ccy, align="center")
            _put(ws, f"C{r}", ending[ccy], "#,##0.000000", F_INPUT)
            _put(ws, f"D{r}", f"={L.PORTFOLIO}!K{lay.cash_first + i}", "#,##0.000000", F_LINK)
            _put(ws, f"E{r}", f"=D{r}-C{r}", "#,##0.000000")
            _put(ws, f"F{r}", "=$B$3", "0.0000")
            _put(ws, f"G{r}", f'=IF(ABS(E{r})<=F{r},"OK","CHECK")', align="center")
            r += 1
    held = p.net_quantities()
    if p.holdings_source not in ("", SRC_TRANSACTIONS):           # nothing to compare when the counts ARE the sums
        for i, (ticker, ccy) in enumerate(positions):
            _put(ws, f"A{r}", SHARE_CHECK)
            _put(ws, f"B{r}", ticker, align="center")
            _put(ws, f"C{r}", held.get((ticker, ccy), 0.0), "General", F_INPUT)
            _put(ws, f"D{r}", f"={L.PORTFOLIO}!S{lay.pos_first + i}", "General", F_LINK)
            _put(ws, f"E{r}", f"=D{r}-C{r}", "0.000000")
            _put(ws, f"F{r}", QTY_TOL, "0.000000", F_INPUT)
            _put(ws, f"G{r}", f'=IF(ABS(E{r})<=F{r},"OK","INFO")', align="center")
            if i == 0:
                _comment(ws, f"G{r}", "INFO (not an error): the statement's Open Positions are the source of truth "
                         "for the share count. A difference to the transaction sum means a stock split / corporate "
                         "action, a transfer, or history from before the first imported statement.", 320, 100)
            r += 1
    last = r - 1
    nav = p.reference_nav()
    if nav is not None:
        _put(ws, f"A{r}", f"Net asset value ({base})")
        _put(ws, f"B{r}", base, align="center")
        _put(ws, f"C{r}", nav, FMT_MONEY, F_INPUT)
        _put(ws, f"D{r}", f"={L.PORTFOLIO}!M{lay.sum_base}", FMT_MONEY, F_LINK)
        _put(ws, f"E{r}", f"=D{r}-C{r}", FMT_MONEY)
        _put(ws, f"F{r}", f"=$E$3*C{r}", FMT_MONEY)
        _put(ws, f"G{r}", f'=IF(ABS(E{r})<=F{r},"OK","INFO")', align="center")
        _comment(ws, f"G{r}", "INFO (not an error): the workbook values holdings at the latest prices/FX, IBKR's NAV "
                 "is at the statement date.", 300, 70)
        last = r
        r += 1
    _put(ws, "A4", "Overall", font=F_BOLD, border=None)
    _put(ws, "B4", f'=IF(COUNTIF(G{first}:G{max(first, last)},"CHECK")=0,"all checks passed",'
                   f'COUNTIF(G{first}:G{max(first, last)},"CHECK")&" check(s) need review")', font=F_BOLD, border=None)
    green = PatternFill("solid", bgColor="C6EFCE", fgColor="C6EFCE")
    red = PatternFill("solid", bgColor="FFC7CE", fgColor="FFC7CE")
    amber = PatternFill("solid", bgColor="FFEB9C", fgColor="FFEB9C")
    rng = f"G{first}:G{max(first, last)}"
    ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"OK"'], fill=green))
    ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"CHECK"'], fill=red))
    ws.conditional_formatting.add(rng, CellIsRule(operator="equal", formula=['"INFO"'], fill=amber))
    ws.sheet_properties.tabColor = "70AD47"
    _print_setup(ws)


def _write_about(ws: Worksheet, p: Portfolio, md: MarketData, lay: Layout, base: str, generated_at: datetime) -> None:
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 110
    _put(ws, "A1", "IBKR Portfolio Tracker - about this workbook", font=F_TITLE, border=None)
    info = [
        ("Account", (f"{p.label}: {', '.join(p.account_ids)}" if p.is_consolidated else
                     f"{p.account} {('(' + p.alias + ')') if p.alias else ''}") + f"  -  base currency {base}"),
        ("Generated", f"{generated_at:%Y-%m-%d %H:%M} by IBKR Portfolio Tracker {__version__}"),
        ("Statement coverage", f"{p.period_start} to {p.period_end}"),
        ("Market data", md.mode),
        ("Valuation date", f"{md.as_of}"),
    ]
    r = 3
    for k, v in info:
        _put(ws, f"A{r}", k, font=F_BOLD, border=None)
        _put(ws, f"B{r}", v, border=None)
        r += 1
    r += 1
    _put(ws, f"A{r}", "Where things are (Portfolio sheet)", font=F_BOLD, border=None)
    r += 1
    index = [
        ("1  Stock transactions", f"A{lay.t_hdr}"), ("2  Dividend payouts", f"A{lay.d_hdr}"),
        ("3  Deposits & withdrawals", f"A{lay.p_hdr}"), ("4  Currency / forex transactions", f"A{lay.f_hdr}"),
        ("5  Transaction extensions (J:P)", f"J{lay.t_hdr}"), ("6  Portfolio positions (J:T)", f"J{lay.pos_hdr}"),
        ("7  Cash balances", f"J{lay.cash_first}"), ("8  Summary (USD)", f"J{lay.sum_usd}"),
        (f"9  Summary ({base})", f"J{lay.sum_base}"), ("10 Benchmark comparison (VUAA)", f"J{lay.bm_hdr}"),
    ]
    for label, ref in index:
        c = ws[f"A{r}"]
        c.value = label
        c.hyperlink = Hyperlink(ref=c.coordinate, location=f"{L.PORTFOLIO}!{ref}", display=label)
        c.font = Font(name=FONT, size=10, color="0563C1", underline="single")
        _put(ws, f"B{r}", f"{L.PORTFOLIO}!{ref}", font=F_NOTE, border=None)
        r += 1
    r += 1
    _put(ws, f"A{r}", "Conventions", font=F_BOLD, border=None)
    r += 1
    notes = [
        ("Colours", "Blue = hard-coded record from the statements or downloaded market data; black = formula; "
                    "green = formula that looks up another sheet; yellow = cell you may overwrite (overrides); "
                    "grey header = helper column."),
        ("Signs", "Quantity and Total are negative for sells. Fee and Withholding Tax are POSITIVE costs "
                  "(IBKR prints them negative), so Gross = Total + Fee."),
        ("Currency conversion", f"FX is quoted as units of currency per 1 USD (USD.{base} = {base} per USD). "
                                "Conversion to USD divides by the rate. Purchase-date rates are as-of lookups "
                                "(last close on or before the date) in the FX sheet."),
        ("USD Amount (Section 3)", f"Spec text says 'Amount * USD.{base}'; for a {base} amount and a ~320 quote this "
                                   "must be a division, which is what the formula does."),
        ("CAGR", "Return^(1/years)-1 with years = deposit-weighted average time since the deposits, measured to the "
                 "valuation date. Interest rows are excluded from deposits."),
        ("Column O (Section 5)", "Current Total converted at the PURCHASE-date rate (as specified) - isolates the "
                                 "local-currency price return; Section 6/8/9 use the latest rate."),
        ("Refreshing", "Use the app's Refresh Prices button: it rewrites only the Prices/FX/Benchmark sheets in place, "
                       "all formulas stay untouched and recalculate. You can also type overrides on the Prices sheet."),
        ("Fee currency", "Forex commission is charged in the account's base currency (Section 4, column H)."),
        ("Share counts", "Quantities in Section 6 come from the 'Open Positions' section of the latest statement "
                         "(blue cells), NOT from the corporate-action section or the transaction sum, so stock splits "
                         "are reflected. Column S = sum of the transactions, column T = factor that converts a "
                         "transaction's quantity into today's shares (used by 'Current Total')."),
        ("VUAA CAGR", "Section 10: every stock transaction replayed into the benchmark ETF; CAGR = (VUAA value / "
                      "invested)^(1/average years since the purchases) - 1, in USD. 'Stocks CAGR' uses the same basis."),
    ]
    for k, v in notes:
        _put(ws, f"A{r}", k, font=F_BOLD, border=None)
        _put(ws, f"B{r}", v, border=None)
        ws[f"B{r}"].alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = 30 if len(v) > 110 else 16
        r += 1
    if p.warnings or md.notes:
        r += 1
        _put(ws, f"A{r}", "Warnings", font=F_BOLD, border=None)
        r += 1
        for w in [*p.warnings, *md.notes]:
            _put(ws, f"B{r}", w, font=F_WARN, border=None)
            ws[f"B{r}"].alignment = Alignment(wrap_text=True, vertical="top")
            ws.row_dimensions[r].height = 30 if len(w) > 110 else 16
            r += 1
    if p.notes:
        r += 1
        _put(ws, f"A{r}", "Notes", font=F_BOLD, border=None)
        r += 1
        for w in p.notes:
            _put(ws, f"B{r}", w, border=None)
            ws[f"B{r}"].alignment = Alignment(wrap_text=True, vertical="top")
            ws.row_dimensions[r].height = 16 * (1 + len(w) // 110)
            r += 1
    r += 1
    _put(ws, f"A{r}", "Statements included", font=F_BOLD, border=None)
    r += 1
    for s in p.statements:
        _put(ws, f"B{r}", f"{s.filename}  [{s.fmt.upper()}]  {s.period_start} - {s.period_end}", border=None)
        r += 1
    ws.sheet_properties.tabColor = "7F7F7F"
    _print_setup(ws)


# ======================================================================================================
# Named ranges
# ======================================================================================================
def _define_names(wb: Workbook, lay: Layout, positions: list[tuple[str, str]], currencies: list[str],
                  fx_cols: list[str]) -> None:
    rng = Layout.rng
    for key, col in L.TRADE_COLS.items():
        _name(wb, "Trade_" + {"name": "Name", "ticker": "Ticker", "qty": "Qty", "price": "Price", "total": "Total",
                              "fee": "Fee", "ccy": "Ccy", "date": "Date"}[key], rng(lay.t_first, lay.t_last, col))
    for key, nm in dict(gross="Gross", cur_price="CurPrice", cur_total="CurTotal", usd_total="USDTotal",
                        usd_gross="USDGross", usd_cur="USDCur", vuaa="VUAAQty").items():
        _name(wb, "Trade_" + nm, rng(lay.t_first, lay.t_last, L.EXT_COLS[key]))
    for key, nm in dict(ticker="Ticker", ccy="Ccy", tx_qty="TxQty", factor="Factor").items():
        _name(wb, "Pos_" + nm, rng(lay.pos_first, lay.pos_last, L.POS_COLS[key]))
    for key, nm in dict(name="Name", ticker="Ticker", qty="Qty", per_share="PerShare", total="Total", tax="Tax",
                        ccy="Ccy", date="Date").items():
        _name(wb, "Div_" + nm, rng(lay.d_first, lay.d_last, L.DIV_COLS[key]))
    for key, nm in dict(desc="Desc", amount="Amount", rate="Rate", usd="USDAmt", date="Date", ccy="Ccy",
                        base="BaseAmt").items():
        _name(wb, "Dep_" + nm, rng(lay.p_first, lay.p_last, L.DEP_COLS[key]))
    for key, nm in dict(bought="Bought", qty="Qty", price="Price", total="Total", fee="Fee", paid="Paid",
                        date="Date", fee_ccy="FeeCcy").items():
        _name(wb, "FXT_" + nm, rng(lay.f_first, lay.f_last, L.FXT_COLS[key]))

    S = L.PX_SETTINGS
    _name(wb, "BaseCcy", f"{L.PRICES}!$B${S['base']}")
    _name(wb, "AsOfDate", f"{L.PRICES}!$B${S['asof']}")
    _name(wb, "DaysPerYear", f"{L.PRICES}!$B${S['days']}")
    _name(wb, "BenchPriceUSD", f"{L.PRICES}!$F${L.PX_BENCH_ROW}")
    n = max(len(positions), 1)
    c = L.PX_STOCK_COLS
    first, last = L.PX_STOCK_HDR + 1, L.PX_STOCK_HDR + n
    _name(wb, "Px_Key", f"{L.PRICES}!${c['key']}${first}:${c['key']}${last}")
    _name(wb, "Px_Used", f"{L.PRICES}!${c['used']}${first}:${c['used']}${last}")
    fc = L.PX_FX_COLS
    n_fxl = len(OV.fx_rows(currencies, fx_cols))
    last_fxl = L.PX_STOCK_HDR + n_fxl
    _name(wb, "FXL_Ccy", f"{L.PRICES}!${fc['ccy']}${first}:${fc['ccy']}${last_fxl}")
    _name(wb, "FXL_Used", f"{L.PRICES}!${fc['used']}${first}:${fc['used']}${last_fxl}")
    # history ranges are (re)written by name_history_ranges - shared with the refresher
    set_history_names(wb)


def set_history_names(wb: Workbook) -> None:
    """(Re)define FX_Dates / FX_Table / FX_Hdr / BM_Dates / BM_USD to the filled area of the history sheets."""
    wsf, wsb = wb[L.FXSHEET], wb[L.BENCH]
    n_cols = 0
    while wsf.cell(1, 2 + n_cols).value not in (None, ""):
        n_cols += 1
    n_rows = 0
    while wsf.cell(2 + n_rows, 1).value not in (None, ""):
        n_rows += 1
    last_col = get_column_letter(1 + max(n_cols, 1))
    last_row = 1 + max(n_rows, 1)
    _name(wb, "FX_Dates", f"{L.FXSHEET}!$A$2:$A${last_row}")
    _name(wb, "FX_Table", f"{L.FXSHEET}!$B$2:${last_col}${last_row}")
    _name(wb, "FX_Hdr", f"{L.FXSHEET}!$B$1:${last_col}$1")
    b_rows = 0
    while wsb.cell(2 + b_rows, 1).value not in (None, ""):
        b_rows += 1
    b_last = 1 + max(b_rows, 1)
    _name(wb, "BM_Dates", f"{L.BENCH}!$A$2:$A${b_last}")
    _name(wb, "BM_USD", f"{L.BENCH}!$B$2:$B${b_last}")


# ======================================================================================================
# hidden state sheet + in-place update of the market-data sheets
# ======================================================================================================
def write_state(wb: Workbook, p: Portfolio, md: MarketData, settings: dict) -> None:
    """Hidden sheet read by the refresher: layout version, archive signature, snapshot of the override cells."""
    if L.STATE in wb.sheetnames:
        del wb[L.STATE]
    ws = wb.create_sheet(L.STATE)
    ws.sheet_state = "hidden"
    fx_ccys = OV.fx_rows(p.currencies(), _fx_columns(md, p.base_currency or "HUF"))
    flat = OV.from_settings(
        dict(settings, price_overrides=md.price_overrides, fx_overrides=md.fx_overrides,
             benchmark_override=md.benchmark_override), p.positions_keys(), fx_ccys)
    values = dict(layout=L.WORKBOOK_VERSION, archive=p.signature(), account=p.account,
                  overrides=json.dumps(flat, sort_keys=True))
    for key, row in OV.STATE_ROWS.items():
        ws.cell(row, 1).value = key
        ws.cell(row, 2).value = values[key]
    ws["D1"] = "Managed by IBKR Portfolio Tracker - do not edit (used by 'Refresh Prices')."


def _clear_area(ws: Worksheet, first_row: int, last_row: int, first_col: int, last_col: int) -> None:
    for row in ws.iter_rows(min_row=first_row, max_row=last_row, min_col=first_col, max_col=last_col):
        for c in row:
            c.value = None
            c.comment = None
            c.hyperlink = None
            c.font = F_NORMAL
            c.fill = PatternFill()
            c.border = Border()
            c.number_format = "General"
            c.alignment = Alignment()


def _fresh_sheet(wb: Workbook, title: str) -> Worksheet:
    idx = wb.sheetnames.index(title)
    wb.remove(wb[title])
    ws = wb.create_sheet(title, idx)
    ws.sheet_view.showGridLines = False
    return ws


def refresh_market_sheets(wb: Workbook, p: Portfolio, md: MarketData, settings: dict,
                          generated_at: Optional[datetime] = None) -> None:
    """Update an existing workbook *in place*: only Prices / FX / Benchmark / About (and the hidden state) are
    rewritten; every formula on Portfolio and Checks is left exactly as it is and recalculates on open."""
    if md.missing_fx:
        raise MissingMarketData("No FX rate available for " + ", ".join(md.missing_fx) + ".")
    base = p.base_currency or "HUF"
    generated_at = generated_at or datetime.now()
    positions, currencies = p.positions_keys(), p.currencies()
    lay = Layout(len(p.trades), len(p.dividends), len(p.cash_flows), len(p.fx_trades), len(positions),
                 len(currencies))
    fx_cols = _fx_columns(md, base)

    wsp = wb[L.PRICES]
    _clear_area(wsp, 1, max(wsp.max_row, 80), 1, 16)
    _write_prices(wsp, p, md, settings, lay, base, positions, currencies, fx_cols, generated_at)
    _write_fx(_fresh_sheet(wb, L.FXSHEET), md, fx_cols, wb)
    _write_bench(_fresh_sheet(wb, L.BENCH), md, wb)
    _write_about(_fresh_sheet(wb, L.ABOUT), p, md, lay, base, generated_at)
    _define_names(wb, lay, positions, currencies, fx_cols)
    write_state(wb, p, md, settings)
    wb.calculation.fullCalcOnLoad = True
