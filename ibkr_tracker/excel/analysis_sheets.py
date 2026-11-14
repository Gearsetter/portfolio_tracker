"""The analysis sheets of the workbook.

``Allocation``
    Donut charts of the portfolio by asset class, stock type, industry sector and currency, the concentration figures and
    the classification of every holding.  The USD values are formulas that read Sections 6 and 7 of the Portfolio sheet,
    so the weights, the charts and the concentration follow every price refresh; the labels (asset class, stock type,
    sector) are values written by the app - from the statement, the cached Yahoo Finance profiles and the user's own
    entries (see :mod:`ibkr_tracker.allocation`).

``Risk`` and ``RiskData``
    Annualised volatility, maximum drawdown, Sharpe / Sortino / Calmar ratios, Value at Risk and the figures against the
    benchmark (see :mod:`ibkr_tracker.analytics` for the definitions).  ``RiskData`` holds the observation days - the
    value of the portfolio, the money paid in and the benchmark price are values written by the app; the returns, the
    wealth index and the drawdown are formulas.  ``Risk`` computes every metric with a formula over those columns, so
    the numbers can be followed (and checked) cell by cell.  Both are rewritten whenever the prices are refreshed.

Only functions that exist in every Excel version are used (``STDEV``, ``PERCENTILE``, ``SLOPE`` ...), no ``STDEV.S`` & co.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from openpyxl import Workbook
from openpyxl.chart import DoughnutChart, LineChart, Reference
from openpyxl.chart.axis import DateAxis
from openpyxl.chart.series import DataPoint
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .. import allocation as A
from .. import analytics as AN
from ..calc import compute_metrics
from ..history import MIN_CAGR_YEARS, History
from ..market.data import MarketData
from ..portfolio import Portfolio
from . import builder as B
from . import layout as L
from .layout import Layout

PALETTE = ["4C78A8", "F58518", "54A24B", "E45756", "72B7B2", "EECA3B", "B279A2", "FF9DA6", "9D755D", "BAB0AC"]
MAX_SLICES = 10                              # donut slices per chart; the smallest groups are merged into "Other (n)"
FMT_WEIGHT = "0.0%"
FMT_RATIO2 = "0.00"
FMT_COUNT = "0"


def _sheet(wb: Workbook, title: str, after: str) -> Worksheet:
    """A new empty sheet called ``title`` - replacing the old one in place, else right after the sheet ``after``."""
    if title in wb.sheetnames:
        idx = wb.sheetnames.index(title)
        wb.remove(wb[title])
    else:
        idx = (wb.sheetnames.index(after) + 1) if after in wb.sheetnames else len(wb.sheetnames)
    ws = wb.create_sheet(title, idx)
    ws.sheet_view.showGridLines = False
    return ws


def write_analysis_sheets(wb: Workbook, p: Portfolio, md: MarketData, settings: dict, lay: Layout,
                          hist: Optional[History], hide: bool = False) -> None:
    """(Re)write the Allocation, Risk and RiskData sheets (after the History sheet).  Shared by the builder and the
    in-place refresher - the Portfolio sheet is never touched."""
    dpy = float(settings.get("days_per_year") or 365)
    base = p.base_currency or "HUF"
    holdings = A.classify(p, compute_metrics(p, md, dpy), settings, md.profiles, hide)
    units = ["base", "usd"] if base != "USD" else ["usd"]
    reports = [AN.risk_report(hist, md, u, AN.risk_free_rate(settings, base if u == "base" else "USD"), None, dpy)
               for u in units]
    write_allocation(_sheet(wb, L.ALLOCATION, L.HISTORY), p, holdings, lay, hide)
    write_risk(_sheet(wb, L.RISK, L.ALLOCATION), _sheet(wb, L.RISKDATA, L.RISK), hist, reports)


# ======================================================================================================
# Allocation
# ======================================================================================================
SLOT = 18                                    # rows of one block: the table and its donut chart
TOP = 4                                      # first row of the first block
BLOCKS = (("asset_class", "By asset class", "E"), ("stock_type", "By stock type", "F"),
          ("sector", "By industry sector", "G"), ("currency", "By currency", "B"))
HOLDING_HEADERS = [("A", "Holding"), ("B", "Currency"), ("C", "USD value"), ("D", "Weight"), ("E", "Asset class"),
                   ("F", "Stock type"), ("G", "Industry sector"), ("H", "Industry"), ("I", "Name"),
                   ("J", "Statement type"), ("K", "Sector from")]


def _donut(ws: Worksheet, title: str, cats: Reference, data: Reference, n: int, anchor: str) -> None:
    ch = DoughnutChart(holeSize=58)
    ch.title = title
    ch.height, ch.width = 8.2, 14.5
    ch.add_data(data, titles_from_data=True)
    ch.set_categories(cats)
    ch.legend.position = "r"                                    # the legend carries the weights (column E) - no labels
    series = ch.series[0]                                       # on the slices: small ones would overlap
    for i in range(n):
        point = DataPoint(idx=i)
        point.graphicalProperties.solidFill = PALETTE[i % len(PALETTE)]
        point.graphicalProperties.line.solidFill = "FFFFFF"
        series.dPt.append(point)
    ws.add_chart(ch, anchor)


def write_allocation(ws: Worksheet, p: Portfolio, holdings: list[A.Holding], lay: Layout, hide: bool = False) -> None:
    ws.sheet_properties.tabColor = "BF8F00"
    for col, w in dict(A=30, B=16, C=14, D=12, E=34, F=24, G=24, H=26, I=34, J=20, K=22).items():
        ws.column_dimensions[col].width = w
    B._put(ws, "A1", "Portfolio allocation - asset class, stock type, industry sector and currency", font=B.F_TITLE,
           border=None)
    if not holdings:
        B._put(ws, "A3", "Nothing to allocate: the portfolio holds no stocks and no cash.", font=B.F_WARN, border=None)
        return
    B._put(ws, "A2", "Shares of the positive USD values of the stocks and cash balances (Sections 6-7 of the Portfolio "
                     "sheet - the USD values are formulas and follow every price refresh). The labels are written by the "
                     "app: change them there (Dashboard > Portfolio analysis > Classify your holdings).",
           font=B.F_NOTE, border=None)

    n = len(holdings)
    conc = TOP + len(BLOCKS) * SLOT
    hdr = conc + 10
    h0, h1 = hdr + 1, hdr + n
    total = h1 + 1
    value = lambda col: f"${col}${h0}:${col}${h1}"                                  # noqa: E731
    pos_row = {k: lay.pos_first + i for i, k in enumerate(p.visible_positions(hide))}
    cash_row = {c: lay.cash_first + i for i, c in enumerate(p.visible_currencies(hide))}

    # ---- the holdings (bottom of the sheet) ------------------------------------------------------------------
    B._put(ws, f"A{hdr - 1}", "Holdings and their classification", font=B.F_BOLD, border=None)
    B._header(ws, hdr, HOLDING_HEADERS)
    B._comment(ws, f"C{hdr}", "USD value of the holding: a formula that reads the USD Value cell of Section 6 (stocks) or "
               "Section 7 (cash) of the Portfolio sheet.", 300, 70)
    B._comment(ws, f"E{hdr}", "Asset class, stock type and sector are values written by the app: the instrument type of "
               "the statement, the sector Yahoo Finance reports (Look up sectors) and your own entries.", 320, 90)
    n_stocks = sum(1 for h in holdings if not h.is_cash)
    for i, h in enumerate(holdings):
        r = h0 + i
        B._put(ws, f"A{r}", h.ticker, font=B.F_INPUT)
        B._put(ws, f"B{r}", h.currency, font=B.F_INPUT, align="center")
        src = pos_row.get((h.ticker, h.currency)) if not h.is_cash else cash_row.get(h.currency)
        if src is None:                                    # (cannot happen: the rows are the same ones) - keep the value
            B._put(ws, f"C{r}", h.usd_value, B.FMT_MONEY, B.F_INPUT)
        else:
            B._put(ws, f"C{r}", f"={L.PORTFOLIO}!O{src}", B.FMT_MONEY, B.F_LINK)
        B._put(ws, f"D{r}", f"=IF(C{r}>0,C{r}/$C${total},0)", FMT_WEIGHT)
        for col, text in (("E", h.asset_class), ("F", h.stock_type), ("G", h.sector), ("H", h.industry), ("I", h.name),
                          ("J", h.ibkr_type), ("K", h.sector_source)):
            B._put(ws, f"{col}{r}", text or None, font=B.F_INPUT)
        ws[f"E{r}"].alignment = Alignment(horizontal="left", indent=1)           # not glued to the right-aligned weight
    for col in "ABCDEFGHIJK":
        B._put(ws, f"{col}{total}", None, font=B.F_BOLD, fill=B.FILL_SUM, border=B.B_SUM)
    B._put(ws, f"A{total}", "Total (positive values)", font=B.F_BOLD, fill=B.FILL_SUM, border=B.B_SUM)
    B._put(ws, f"C{total}", f'=SUMIF(C{h0}:C{h1},">0")', B.FMT_MONEY, B.F_BOLD, B.FILL_SUM, B.B_SUM)
    B._put(ws, f"D{total}", f"=SUM(D{h0}:D{h1})", FMT_WEIGHT, B.F_BOLD, B.FILL_SUM, B.B_SUM)
    B._put(ws, f"A{total + 1}", "Summary (USD) - Section 8 of the Portfolio sheet", border=None)
    B._put(ws, f"C{total + 1}", f"={L.PORTFOLIO}!O{lay.sum_usd}", B.FMT_MONEY, B.F_LINK, border=None)
    B._put(ws, f"A{total + 2}", "Not in the table above", border=None)
    B._put(ws, f"C{total + 2}", f"=ROUND(C{total + 1}-C{total},6)", B.FMT_MONEY, border=None)     # (no -0.00 from float noise)
    B._put(ws, f"D{total + 2}", "negative balances, balances the view filter hides, closed positions", font=B.F_NOTE,
           border=None)

    # ---- one block per grouping: table and donut chart -------------------------------------------------------
    for k, (by, title, label_col) in enumerate(BLOCKS):
        s = TOP + k * SLOT
        allocation = A.allocate(holdings, by)
        slices = A.chart_slices(allocation, MAX_SLICES)
        merged = len(allocation.slices) > len(slices)
        B._put(ws, f"A{s}", title, font=B.F_BOLD, border=None)
        B._header(ws, s + 1, [("A", "Group"), ("B", "USD value"), ("C", "Weight"), ("D", "Holdings"), ("E", "Chart label")],
                  helper={"E"})
        first = s + 2
        last = first + len(slices) - 1
        for i, sl in enumerate(slices):
            r = first + i
            B._put(ws, f"A{r}", sl.label, font=B.F_INPUT)
            if merged and i == len(slices) - 1:                         # "Other (n)": whatever the listed groups leave
                B._put(ws, f"B{r}", f"=$C${total}-SUM(B{first}:B{r - 1})" if i else f"=$C${total}", B.FMT_MONEY)
                B._put(ws, f"D{r}", f'=COUNTIF({value("C")},">0")-SUM(D{first}:D{r - 1})' if i else
                       f'=COUNTIF({value("C")},">0")', FMT_COUNT)
            else:
                B._put(ws, f"B{r}", f'=SUMIFS({value("C")},{value(label_col)},$A{r},{value("C")},">0")', B.FMT_MONEY)
                B._put(ws, f"D{r}", f'=COUNTIFS({value(label_col)},$A{r},{value("C")},">0")', FMT_COUNT)
            B._put(ws, f"C{r}", f"=IF($C${total}>0,B{r}/$C${total},0)", FMT_WEIGHT)
            # the legend text "Group  ·  12.3%" (FIXED, not TEXT: a TEXT format code depends on the language of Excel)
            B._put(ws, f"E{r}", f'=A{r}&"  ·  "&FIXED(C{r}*100,1,TRUE)&"%"', fill=B.FILL_HELP)
            ws[f"E{r}"].alignment = Alignment(horizontal="left", indent=1)       # not glued to the right-aligned count
        tot = last + 1
        for col in "ABCD":
            B._put(ws, f"{col}{tot}", None, font=B.F_BOLD, fill=B.FILL_SUM, border=B.B_SUM)
        B._put(ws, f"A{tot}", "Total", font=B.F_BOLD, fill=B.FILL_SUM, border=B.B_SUM)
        B._put(ws, f"B{tot}", f"=SUM(B{first}:B{last})", B.FMT_MONEY, B.F_BOLD, B.FILL_SUM, B.B_SUM)
        B._put(ws, f"C{tot}", f"=SUM(C{first}:C{last})", FMT_WEIGHT, B.F_BOLD, B.FILL_SUM, B.B_SUM)
        B._put(ws, f"D{tot}", f"=SUM(D{first}:D{last})", FMT_COUNT, B.F_BOLD, B.FILL_SUM, B.B_SUM)
        B._put(ws, f"E{tot}", f'=IF(ABS(B{tot}-$C${total})<0.005,"","<- does not add up to the holdings total: a label '
                              f'is missing from this list")', font=B.F_WARN, border=None)
        if merged:
            B._put(ws, f"A{tot + 1}", f"The smallest groups are merged into Other (at most {MAX_SLICES} slices).",
                   font=B.F_NOTE, border=None)
        _donut(ws, f"{title.replace('By ', 'Allocation by ')} (USD)", Reference(ws, min_col=5, min_row=first, max_row=last),
               Reference(ws, min_col=2, min_row=s + 1, max_row=last), len(slices), f"F{s}")

    # ---- concentration ---------------------------------------------------------------------------------------
    s0, s1 = h0, h0 + n_stocks - 1                                       # the stock rows (cash lines come last)
    c0, c1 = s1 + 1, h1
    B._put(ws, f"A{conc}", "Concentration", font=B.F_BOLD, border=None)
    B._header(ws, conc + 1, [("A", "Measure"), ("B", "Value")])
    if n_stocks:
        w = f"$D${s0}:$D${s1}"
        v = f"$C${s0}:$C${s1}"
        top = lambda k: "+".join(f"IFERROR(LARGE({w},{i}),0)" for i in range(1, k + 1))      # noqa: E731
        rows = [("Stock positions", f'=COUNTIF({v},">0")', FMT_COUNT),
                ("Largest position", f"=IFERROR(MAX({w}),0)", FMT_WEIGHT),
                ("Top 3 positions", f"={top(3)}", FMT_WEIGHT), ("Top 5 positions", f"={top(5)}", FMT_WEIGHT),
                ("Effective positions", f'=IFERROR(SUMIF({v},">0")^2/SUMPRODUCT(({v}>0)*{v}*{v}),"n/a")', FMT_RATIO2)]
    else:
        rows = [("Stock positions", 0, FMT_COUNT), ("Largest position", "n/a", FMT_WEIGHT),
                ("Top 3 positions", "n/a", FMT_WEIGHT), ("Top 5 positions", "n/a", FMT_WEIGHT),
                ("Effective positions", "n/a", FMT_RATIO2)]
    cash = f'=IFERROR(SUMIF($C${c0}:$C${c1},">0")/$C${total},0)' if c0 <= c1 else 0
    rows.append(("Cash share", cash, FMT_WEIGHT))
    for i, (label, formula, fmt) in enumerate(rows, start=2):
        B._put(ws, f"A{conc + i}", label)
        B._put(ws, f"B{conc + i}", formula, fmt)
    if n_stocks:
        B._put(ws, f"C{conc + 3}", f"=IFERROR(INDEX($A${s0}:$A${s1},MATCH(B{conc + 3},$D${s0}:$D${s1},0)),\"\")",
               font=B.F_NOTE, border=None)
        ws[f"C{conc + 3}"].alignment = Alignment(horizontal="left", indent=1)
    B._comment(ws, f"A{conc + 6}", "Effective positions = 1 / sum of the squared stock weights (the stocks' own weights): "
               "10 equally large positions = 10.0, one dominant position = close to 1.", 320, 80)
    B._comment(ws, f"A{conc + 3}", "Largest, top 3 and top 5 are shares of the whole portfolio (stocks + cash).", 280, 50)
    B._print_setup(ws, landscape=True)


# ======================================================================================================
# Risk and RiskData
# ======================================================================================================
DATA_HDR, DATA_FIRST = 6, 7                  # RiskData: header row and first observation row
BLOCK_W = 18                                 # columns of one unit's block (17 + one empty)
BLOCK_COLS = ["date", "value", "flow", "ret", "index", "peak", "dd", "vol", "bpx", "bret", "bidx", "bpeak", "bdd", "bvol",
              "pret_b", "active", "back"]
FMT_RET = "0.00%"
SQ = "SQRT(252)"


class _Unit:
    """Where the observation table of one unit (base currency / USD) lives on RiskData, and its columns on Risk."""

    def __init__(self, rep: AN.RiskReport, j: int):
        self.rep, self.j = rep, j
        self.c0 = 1 + j * BLOCK_W
        self.n = len(rep.frame)
        self.first, self.last = DATA_FIRST, DATA_FIRST + self.n - 1
        self.cp = get_column_letter(2 + 2 * j)               # Risk sheet: the portfolio's column ...
        self.cb = get_column_letter(3 + 2 * j)               # ... and the benchmark's
        self.bench = bool(rep.benchmark is not None and rep.benchmark.volatility is not None)
        self.relative = rep.relative is not None

    def col(self, name: str) -> str:
        return get_column_letter(self.c0 + BLOCK_COLS.index(name))

    def rng(self, name: str) -> str:
        c = self.col(name)
        return f"{L.RISKDATA}!${c}${self.first}:${c}${self.last}"

    def end(self, name: str) -> str:
        c = self.col(name)
        return f"{L.RISKDATA}!${c}${self.last}"


def _line_chart(wd: Worksheet, ws: Worksheet, title: str, u: _Unit, cols: list[str], anchor: str, y_format: str) -> None:
    ch = LineChart()
    ch.title = title
    ch.height, ch.width = 8.6, 16.5
    ch.y_axis.number_format = y_format
    for name in cols:
        c = BLOCK_COLS.index(name) + u.c0
        ch.add_data(Reference(wd, min_col=c, min_row=DATA_HDR, max_row=u.last), titles_from_data=True)
    ch.set_categories(Reference(wd, min_col=u.c0, min_row=u.first, max_row=u.last))
    ch.x_axis = DateAxis(crossAx=100)
    ch.x_axis.number_format = "yyyy-mm"
    ch.x_axis.majorTimeUnit = "months"
    ch.y_axis.crossAx = 500
    ch.x_axis.delete = False
    ch.y_axis.delete = False
    ch.x_axis.tickLblPos = "low"                                 # (a drawdown is negative: the dates stay below the curve)
    ch.legend.position = "b"
    for series in ch.series:
        series.smooth = False
        series.marker.symbol = "none"
        series.graphicalProperties.line.width = 19050
    ws.add_chart(ch, anchor)


def write_risk(ws: Worksheet, wd: Worksheet, hist: Optional[History], reports: list[AN.RiskReport]) -> None:
    ws.sheet_properties.tabColor = "C55A11"
    wd.sheet_properties.tabColor = "C55A11"
    ws.column_dimensions["A"].width = 38
    for col in "BCDE":
        ws.column_dimensions[col].width = 17
    ws.column_dimensions["F"].width = 100
    B._put(ws, "A1", "Risk and risk-adjusted return", font=B.F_TITLE, border=None)
    B._put(wd, "A1", "Risk data - the observation days behind the Risk sheet", font=B.F_TITLE, border=None)
    ok = [r for r in reports if r.ok]
    if not ok:
        message = next((r.message for r in reports if r.message), "There is no history to analyse.")
        B._put(ws, "A3", message, font=B.F_WARN, border=None)
        B._put(wd, "A3", "No observation days yet - see the Risk sheet.", font=B.F_NOTE, border=None)
        return
    units = [_Unit(r, j) for j, r in enumerate(ok)]
    B._put(ws, "A2", "Time-weighted daily returns of the day-by-day history: deposits and withdrawals are taken out, so the "
                     "figures measure the investing, not the timing of the payments. Annualised with 252 trading days. "
                     "Every metric is a formula over the RiskData sheet; the risk-free rate is the one entered in the app "
                     "(Dashboard > Portfolio analysis > Risk & return) - type another one here to see its effect.",
           font=B.F_NOTE, border=None)
    notes = list(dict.fromkeys(n for r in ok for n in r.notes))
    skipped = [r for r in reports if not r.ok and r.message]
    if skipped:
        notes.append(f"{skipped[0].currency}: {skipped[0].message}")
    if notes:
        B._put(ws, "A3", "  ".join(notes), font=B.F_WARN, border=None)
    rep0 = units[0].rep
    bench_name = (rep0.benchmark_symbol or "Benchmark").split(".")[0]

    # ---- the observation tables (RiskData) -----------------------------------------------------------------
    for u in units:
        _write_data_block(wd, u, bench_name)
    wd.freeze_panes = f"A{DATA_FIRST}"
    B._print_setup(wd, landscape=True, title_rows=f"{DATA_HDR}:{DATA_HDR}")
    B._put(wd, "A2", "Written by the app: date, value, net deposits and the benchmark price (blue). Everything else is a "
                     "formula: return = value / (previous value + deposits) - 1, wealth index = product of (1 + return), "
                     "drawdown = index / highest index so far - 1.", font=B.F_NOTE, border=None)

    # ---- the metrics (Risk) ----------------------------------------------------------------------------------
    spec = max((AN.metric_rows(u.rep) for u in units), key=len)
    head = [("A", "Metric")]
    for u in units:
        head += [(u.cp, f"Portfolio ({u.rep.currency})"), (u.cb, f"{bench_name} ({u.rep.currency})")]
    head.append(("F", "What it means"))
    B._header(ws, 5, head)
    rows: dict[str, int] = {}
    r = 6
    for key, label in (("start", "Window start (first transaction)"), ("end", "Window end (valuation date)"),
                       ("years", "Years in the window"), ("obs", "Observation days"),
                       ("rf", "Risk-free rate (annual)"), ("rfd", "Risk-free rate (daily)"),
                       ("coverage", "Stock value with a recent price")):
        rows[key] = r
        B._put(ws, f"A{r}", label, fill=B.FILL_HELP)
        r += 1
    group = None
    metric_keys: list[AN.MetricRow] = []
    for row in spec:
        if row.group != group:
            group = row.group
            r += 1
            B._put(ws, f"A{r}", group, font=B.F_BOLD, fill=B.FILL_SUM, border=B.B_SUM)
            for u in units:
                B._put(ws, f"{u.cp}{r}", None, fill=B.FILL_SUM, border=B.B_SUM)
                B._put(ws, f"{u.cb}{r}", None, fill=B.FILL_SUM, border=B.B_SUM)
            B._put(ws, f"F{r}", None, fill=B.FILL_SUM, border=B.B_SUM)
            r += 1
        if row.key == "mdd_period":                                    # three dates instead of the text of the app
            for k2, label2 in (("mdd_peak", "Maximum drawdown - peak day"), ("mdd_trough", "Maximum drawdown - trough day"),
                               ("mdd_recovery", "Maximum drawdown - back at the peak")):
                rows[k2] = r
                metric_keys.append(AN.MetricRow(row.group, k2, label2, None, None, AN.TEXT,
                                                row.help if k2 == "mdd_peak" else ""))
                r += 1
            continue
        rows[row.key] = r
        metric_keys.append(row)
        r += 1
    last_row = r - 1

    start = hist.start if hist is not None else None
    end = hist.end if hist is not None else None
    for u in units:
        _write_metrics(ws, u, rows, metric_keys, start, end)
    for row in metric_keys:
        B._put(ws, f"A{rows[row.key]}", row.label)
        B._put(ws, f"F{rows[row.key]}", row.help or None, font=B.F_NOTE, border=None)
        ws[f"F{rows[row.key]}"].alignment = Alignment(horizontal="left", indent=1)
    B._put(ws, f"F{rows['rf']}", "The risk-free rate of the unit's currency, entered in the app; used by Sharpe, Sortino, alpha "
                                 "and Treynor.", font=B.F_NOTE, border=None)
    B._put(ws, f"F{rows['years']}", "(end - start) / days per year (Prices sheet) - the annualised return appears once the "
                                    "window is three months long.", font=B.F_NOTE, border=None)
    B._put(ws, f"F{rows['coverage']}", "Below 90 % the metrics understate the swings (carried-forward prices); below 50 % "
                                       "they are not computed.", font=B.F_NOTE, border=None)
    for key in ("rf", "years", "coverage"):
        ws[f"F{rows[key]}"].alignment = Alignment(horizontal="left", indent=1)

    # ---- charts ----------------------------------------------------------------------------------------------
    anchor = last_row + 3
    for u in units:
        B._put(ws, f"A{anchor - 1}", f"Charts - {u.rep.currency}", font=B.F_BOLD, border=None)
        dd_cols = ["dd"] + (["bdd"] if u.bench else [])
        vol_cols = ["vol"] + (["bvol"] if u.bench and u.n >= AN.ROLLING_WINDOW else [])
        _line_chart(wd, ws, f"Drawdown ({u.rep.currency})", u, dd_cols, f"A{anchor}", "0%")
        if u.n >= AN.ROLLING_WINDOW:
            _line_chart(wd, ws, f"Rolling volatility, {AN.ROLLING_WINDOW} trading days ({u.rep.currency})", u, vol_cols,
                        f"E{anchor}", "0%")
        anchor += 21
    B._print_setup(ws, landscape=True)


def _write_data_block(wd: Worksheet, u: _Unit, bench_name: str) -> None:
    """One unit's observation table on RiskData."""
    rep, frame = u.rep, u.rep.frame
    ccy = rep.currency
    col = {name: u.col(name) for name in BLOCK_COLS}
    B._put(wd, f"{col['date']}4", f"{ccy} - {len(frame)} observation days", font=B.F_BOLD, border=None)
    for name, width in zip(BLOCK_COLS, (12, 15, 15, 11, 12, 11, 11, 15, 15, 12, 14, 12, 12, 15, 15, 12, 14)):
        wd.column_dimensions[col[name]].width = width
    titles = {"date": "Date", "value": f"Value ({ccy})", "flow": f"Net deposits ({ccy})", "ret": "Return",
              "index": "Wealth index", "peak": "Peak", "dd": "Portfolio drawdown",
              "vol": f"Portfolio rolling volatility ({AN.ROLLING_WINDOW} days)",
              "bpx": f"{bench_name} price ({ccy})", "bret": f"{bench_name} return", "bidx": f"{bench_name} wealth index",
              "bpeak": f"{bench_name} peak", "bdd": f"{bench_name} drawdown",
              "bvol": f"{bench_name} rolling volatility ({AN.ROLLING_WINDOW} days)",
              "pret_b": f"Portfolio return on the {bench_name} days", "active": f"Return minus {bench_name}",
              "back": "Back at the peak (helper)"}
    shown = [n for n in BLOCK_COLS if u.bench or n not in ("bpx", "bret", "bidx", "bpeak", "bdd", "bvol", "pret_b", "active")]
    for name in shown:
        B._put(wd, f"{col[name]}{DATA_HDR}", titles[name], font=B.F_BOLD, fill=B.FILL_HELP if name == "back" else B.FILL_HDR,
               border=B.B_HDR, align="center")
        wd[f"{col[name]}{DATA_HDR}"].alignment = Alignment(wrap_text=True, horizontal="center", vertical="center")
    wd.row_dimensions[DATA_HDR].height = 44

    # helper values of the "back at the peak" column: where the deepest drawdown ends and the peak it fell from
    d_rng, f_rng = u.rng("dd"), u.rng("peak")
    B._put(wd, f"{col['index']}3", "Trough is observation #", font=B.F_NOTE, border=None)
    B._put(wd, f"{col['peak']}3", "Peak at the trough", font=B.F_NOTE, border=None)
    pos, peakval = f"${col['index']}$4", f"${col['peak']}$4"
    B._put(wd, f"{col['index']}4", f"=MATCH(MIN({d_rng}),{d_rng},0)", FMT_COUNT, border=None)
    B._put(wd, f"{col['peak']}4", f"=INDEX({f_rng},{pos})", "0.0000", border=None)

    bench_ret = frame["bench_ret"].to_numpy(dtype=float)
    bench_px = frame["bench_price"].to_numpy(dtype=float)
    finite = lambda x: x == x and abs(x) != float("inf")                    # noqa: E731
    valid = [u.bench and finite(x) for x in bench_ret]
    started = False
    window = AN.ROLLING_WINDOW
    for i in range(u.n):
        r = DATA_FIRST + i
        ts = frame.index[i]
        prev = r - 1
        B._put(wd, f"{col['date']}{r}", B._dt(ts.date()), B.FMT_DATE, B.F_INPUT, border=None)
        B._put(wd, f"{col['value']}{r}", float(frame["value"].iloc[i]), B.FMT_MONEY, B.F_INPUT, border=None)
        B._put(wd, f"{col['flow']}{r}", float(frame["flow"].iloc[i]), B.FMT_MONEY, B.F_INPUT, border=None)
        v, fl, ret, idx, pk, dd = (f"{col[x]}{r}" for x in ("value", "flow", "ret", "index", "peak", "dd"))
        if i == 0:                                       # the first observation starts from nothing: V / F - 1
            B._put(wd, ret, f"={v}/{fl}-1", FMT_RET, border=None)
            B._put(wd, idx, f"=1+{ret}", "0.0000", border=None)
            B._put(wd, pk, f"=MAX(1,{idx})", "0.0000", border=None)
        else:
            B._put(wd, ret, f"={v}/({col['value']}{prev}+{fl})-1", FMT_RET, border=None)
            B._put(wd, idx, f"={col['index']}{prev}*(1+{ret})", "0.0000", border=None)
            B._put(wd, pk, f"=MAX({col['peak']}{prev},{idx})", "0.0000", border=None)
        B._put(wd, dd, f"={idx}/{pk}-1", FMT_RET, border=None)
        if i + 1 >= window:
            B._put(wd, f"{col['vol']}{r}", f"=STDEV({col['ret']}{r - window + 1}:{ret})*{SQ}", FMT_RET, border=None)
        B._put(wd, f"{col['back']}{r}", f'=IF(AND(ROW()-ROW(${col["index"]}${DATA_FIRST})+1>{pos},{idx}>={peakval}),'
                                         f'{col["date"]}{r},"")', B.FMT_DATE, border=None)
        if not u.bench:
            continue
        if finite(bench_px[i]):
            B._put(wd, f"{col['bpx']}{r}", float(bench_px[i]), B.FMT_PRICE, B.F_INPUT, border=None)
        if valid[i]:
            first_valid = not started
            started = True
            b_idx, b_pk, b_dd = f"{col['bidx']}{r}", f"{col['bpeak']}{r}", f"{col['bdd']}{r}"
            B._put(wd, f"{col['bret']}{r}", f"={col['bpx']}{r}/{col['bpx']}{prev}-1", FMT_RET, border=None)
            B._put(wd, b_idx, (f"=1+{col['bret']}{r}" if first_valid else f"={col['bidx']}{prev}*(1+{col['bret']}{r})"),
                   "0.0000", border=None)
            B._put(wd, f"{col['pret_b']}{r}", f"={ret}", FMT_RET, border=None)
            B._put(wd, f"{col['active']}{r}", f"={col['pret_b']}{r}-{col['bret']}{r}", FMT_RET, border=None)
        elif started:                                    # a gap in the benchmark prices: the index stays where it was
            b_idx, b_pk, b_dd = f"{col['bidx']}{r}", f"{col['bpeak']}{r}", f"{col['bdd']}{r}"
            B._put(wd, b_idx, f"={col['bidx']}{prev}", "0.0000", border=None)
        else:
            continue
        if valid[i] and first_valid:
            B._put(wd, b_pk, f"=MAX(1,{b_idx})", "0.0000", border=None)
        else:
            B._put(wd, b_pk, f"=MAX({col['bpeak']}{prev},{b_idx})", "0.0000", border=None)
        B._put(wd, b_dd, f"={b_idx}/{b_pk}-1", FMT_RET, border=None)
        # the rolling volatility of the benchmark needs a full window of benchmark returns
        if i + 1 >= window and all(valid[i - window + 1:i + 1]):
            B._put(wd, f"{col['bvol']}{r}", f"=STDEV({col['bret']}{r - window + 1}:{col['bret']}{r})*{SQ}", FMT_RET,
                   border=None)


def _write_metrics(ws: Worksheet, u: _Unit, rows: dict[str, int], metrics: list[AN.MetricRow], start: Optional[date],
                   end: Optional[date]) -> None:
    """The column(s) of one unit on the Risk sheet: the window, the risk-free rate and every metric as a formula."""
    rep = u.rep
    cp, cb = u.cp, u.cb
    P = lambda key: f"${cp}${rows[key]}"                                    # noqa: E731 - a metric's cell, portfolio
    Bn = lambda key: f"${cb}${rows[key]}"                                   # noqa: E731 - ... benchmark
    m = P("rfd")
    D, E, G = u.rng("ret"), u.rng("index"), u.rng("dd")
    J, K, X, Q, N = u.rng("bret"), u.rng("bidx"), u.rng("bdd"), u.rng("pret_b"), u.rng("active")
    NA = "n/a"

    def put(col: str, key: str, value, fmt: Optional[str] = None, font=B.F_NORMAL) -> None:
        B._put(ws, f"{col}{rows[key]}", value, fmt, font)

    # window and inputs
    put(cp, "start", B._dt(start) if start else None, B.FMT_DATE, B.F_INPUT)
    put(cp, "end", B._dt(end) if end else None, B.FMT_DATE, B.F_INPUT)
    put(cp, "years", f'=IFERROR(({P("end")}-{P("start")})/DaysPerYear,"n/a")', "0.00")
    put(cp, "obs", f"=COUNT({D})", FMT_COUNT)
    put(cp, "rf", float(rep.risk_free), "0.00%", B.F_INPUT)
    put(cp, "rfd", f"=(1+{P('rf')})^(1/252)-1", "0.0000%")
    put(cp, "coverage", float(rep.price_coverage if rep.price_coverage is not None else 1.0), "0.0%", B.F_INPUT)
    if u.bench:
        put(cb, "obs", f"=COUNT({J})", FMT_COUNT)
        put(cb, "rf", f"={P('rf')}", "0.00%")
        put(cb, "rfd", f"={P('rfd')}", "0.0000%")

    def downside(rng: str) -> str:
        return f'SQRT(SUMPRODUCT(({rng}<>"")*({rng}<{m})*({rng}-{m})^2)/COUNT({rng}))'

    def cvar(rng: str) -> str:
        q = f"PERCENTILE({rng},0.05)"
        return (f'IFERROR(MAX(-SUMPRODUCT(({rng}<={q})*({rng}<>"")*{rng})/SUMPRODUCT(({rng}<={q})*({rng}<>"")),0),"n/a")')

    both = {   # key: (portfolio formula, benchmark formula, number format)
        "cumulative": (f"={u.end('index')}-1", f'=IFERROR({u.end("bidx")}-1,"n/a")', FMT_RET),
        "annualized": (f'=IF(AND(ISNUMBER({P("years")}),1+{P("cumulative")}>0),IF({P("years")}>={MIN_CAGR_YEARS},'
                       f'(1+{P("cumulative")})^(1/{P("years")})-1,"n/a"),"n/a")',
                       f'=IF(AND(ISNUMBER({P("years")}),ISNUMBER({Bn("cumulative")})),IF({P("years")}>={MIN_CAGR_YEARS},'
                       f'(1+{Bn("cumulative")})^(1/{P("years")})-1,"n/a"),"n/a")', FMT_RET),
        "volatility": (f"=STDEV({D})*{SQ}", f"=STDEV({J})*{SQ}", FMT_RET),
        "downside": (f"={downside(D)}*{SQ}", f"={downside(J)}*{SQ}", FMT_RET),
        "mdd": (f"=MIN({G})", f"=MIN({X})", FMT_RET),
        "current_dd": (f"={u.end('dd')}", f"={u.end('bdd')}", FMT_RET),
        "var95": (f"=MAX(-PERCENTILE({D},0.05),0)", f"=MAX(-PERCENTILE({J},0.05),0)", FMT_RET),
        "cvar95": (f"={cvar(D)}", f"={cvar(J)}", FMT_RET),
        "best": (f"=MAX({D})", f"=MAX({J})", FMT_RET),
        "worst": (f"=MIN({D})", f"=MIN({J})", FMT_RET),
        "positive": (f'=COUNTIF({D},">0")/COUNT({D})', f'=COUNTIF({J},">0")/COUNT({J})', FMT_RET),
        "sharpe": (f'=IFERROR((AVERAGE({D})-{m})/STDEV({D})*{SQ},"n/a")',
                   f'=IFERROR((AVERAGE({J})-{m})/STDEV({J})*{SQ},"n/a")', FMT_RATIO2),
        "sortino": (f'=IFERROR((AVERAGE({D})-{m})/{downside(D)}*{SQ},"n/a")',
                    f'=IFERROR((AVERAGE({J})-{m})/{downside(J)}*{SQ},"n/a")', FMT_RATIO2),
        "calmar": (f'=IF(AND(ISNUMBER({P("annualized")}),{P("mdd")}<0),{P("annualized")}/ABS({P("mdd")}),"n/a")',
                   f'=IF(AND(ISNUMBER({Bn("annualized")}),{Bn("mdd")}<0),{Bn("annualized")}/ABS({Bn("mdd")}),"n/a")',
                   FMT_RATIO2),
    }
    relative = {}  # portfolio against the benchmark: (portfolio formula, the benchmark's own value) - only with a benchmark
    if "beta" in rows:
        relative = {
            "beta": (f'=IFERROR(SLOPE({Q},{J}),"n/a")', 1, FMT_RATIO2),
            "correlation": (f'=IFERROR(CORREL({Q},{J}),"n/a")', 1, FMT_RATIO2),
            "alpha": (f'=IFERROR((AVERAGE({Q})-{m}-{P("beta")}*(AVERAGE({J})-{m}))*252,"n/a")', 0, FMT_RET),
            "tracking": (f'=IFERROR(STDEV({N})*{SQ},"n/a")', 0, FMT_RET),
            "info": (f'=IFERROR(AVERAGE({N})/STDEV({N})*{SQ},"n/a")', None, FMT_RATIO2),
            "treynor": (f'=IFERROR(IF(ABS({P("beta")})>0.000000000001,(AVERAGE({Q})-{m})*252/{P("beta")},"n/a"),"n/a")',
                        None, FMT_RET),
        }
    dates = u.rng("date")
    drawdown_dates = {
        "mdd_peak": (f'=IF({P("mdd")}=0,"",IFERROR(INDEX({dates},MATCH(INDEX({u.rng("peak")},MATCH({P("mdd")},{G},0)),'
                     f'{E},0)),{P("start")}))'),
        "mdd_trough": f'=IF({P("mdd")}=0,"",INDEX({dates},MATCH({P("mdd")},{G},0)))',
        "mdd_recovery": f'=IF({P("mdd")}=0,"",IF(COUNT({u.rng("back")})=0,"not recovered yet",MIN({u.rng("back")})))',
    }
    for row in metrics:
        key = row.key
        if key in both:
            pf, bf, fmt = both[key]
            put(cp, key, pf, fmt)
            put(cb, key, bf if u.bench else NA, fmt, B.F_NORMAL if u.bench else B.F_NOTE)
        elif key in relative:
            pf, own, fmt = relative[key]
            if u.relative:
                put(cp, key, pf, fmt)
                put(cb, key, own, fmt)
            else:
                put(cp, key, NA, None, B.F_NOTE)
                put(cb, key, None)
        elif key in drawdown_dates:
            put(cp, key, drawdown_dates[key], B.FMT_DATE)
            ws[f"{cp}{rows[key]}"].alignment = Alignment(horizontal="right")
            put(cb, key, None)
