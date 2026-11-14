"""The generated workbook: geometry required by the task, formulas instead of values, naming, colour code,
recalculation (LibreOffice) against the Python oracle."""
from __future__ import annotations

import re
from datetime import datetime

import pytest
from openpyxl import load_workbook
from openpyxl.formula import Tokenizer

from conftest import TODAY, needs_lo
from fakes import FakeProvider
from helpers import compare_portfolio_sheet, error_cells, layout_for, sheet_values
from ibkr_tracker.calc import compute_metrics
from ibkr_tracker.config import DEFAULT_SETTINGS
from ibkr_tracker.excel import build_workbook, embed_values
from ibkr_tracker.excel.layout import WORKBOOK_VERSION
from ibkr_tracker.market import MarketDataService
from ibkr_tracker.reconcile import SHARE_CHECK
from ibkr_tracker.storage import Store

SETTINGS = dict(DEFAULT_SETTINGS, price_source="statement", recalc_with_libreoffice=False)


# ---- fixtures -------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def md_off(pf_csv, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("xl_off") / "t.db")
    return MarketDataService(store).build(pf_csv, SETTINGS)


@pytest.fixture(scope="module")
def md_on(pf_csv, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("xl_on") / "t.db")
    s = dict(DEFAULT_SETTINGS, recalc_with_libreoffice=False)
    svc = MarketDataService(store, FakeProvider(last_day=TODAY), today=lambda: TODAY)
    svc.refresh(pf_csv, s)
    return svc.build(pf_csv, s)


@pytest.fixture(scope="module")
def wb(pf_csv, md_off):
    return build_workbook(pf_csv, md_off, SETTINGS, generated_at=datetime(2026, 11, 2, 12, 0))


@pytest.fixture(scope="module")
def ws(wb):
    return wb["Portfolio"]


@pytest.fixture(scope="module")
def lay(pf_csv):
    return layout_for(pf_csv)


def blank(ws, rows, cols):
    return [f"{c}{r}" for r in rows for c in cols if ws[f"{c}{r}"].value not in (None, "")]


def formulas(wb):
    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    yield sheet.title, c.coordinate, c.value


# ======================================================================================================
# structure required by the task
# ======================================================================================================
def test_sheets(wb):
    assert wb.sheetnames == ["Portfolio", "Prices", "FX", "Benchmark", "History", "Allocation", "Risk", "RiskData", "Checks", "About",
                           "_state"]
    assert wb["_state"].sheet_state == "hidden" and wb.active.title == "Portfolio"
    assert wb.calculation.fullCalcOnLoad is True
    assert wb.properties.title == "IBKR portfolio U24013787"


def test_section_geometry_of_the_sample(lay):
    # 22 stock trades, 10 dividend events, 14 cash flows, 62 forex trades, 6 positions, 3 cash currencies
    assert (lay.t_first, lay.t_last) == (2, 23)
    assert (lay.d_hdr, lay.d_first, lay.d_last) == (29, 30, 39)
    assert (lay.p_hdr, lay.p_first, lay.p_last) == (45, 46, 59)
    assert (lay.f_hdr, lay.f_first, lay.f_last) == (65, 66, 127)
    assert (lay.pos_hdr, lay.pos_first, lay.pos_last) == (29, 30, 35)
    assert (lay.cash_first, lay.cash_last) == (36, 38)
    assert (lay.sum_usd, lay.sum_base, lay.bm_hdr) == (40, 42, 44)


def test_five_empty_rows_between_sections_1_to_4(ws, lay):
    left = "ABCDEFGH"
    for last, next_hdr in ((lay.t_last, lay.d_hdr), (lay.d_last, lay.p_hdr), (lay.p_last, lay.f_hdr)):
        assert next_hdr - last == 6
        assert blank(ws, range(last + 1, next_hdr), left) == []                      # 5 empty rows
        assert ws[f"A{next_hdr}"].value and ws[f"A{last}"].value                     # ... and nothing else is empty
    assert blank(ws, range(1, lay.f_last + 1), "I") == []                           # spacer column


def test_right_hand_block_spacing(ws, lay):
    right = "JKLMNOPQRS"
    assert lay.pos_hdr - lay.t_last == 6 and blank(ws, range(lay.t_last + 1, lay.pos_hdr), right) == []    # 5 empty rows
    assert lay.cash_first == lay.pos_last + 1                                       # S7 immediately after S6
    assert ws[f"J{lay.pos_last}"].value == "NVO" and ws[f"J{lay.cash_first}"].value == "HUF"
    assert [ws[f"J{r}"].value for r in range(lay.cash_first, lay.cash_last + 1)] == ["HUF", "USD", "EUR"]
    assert blank(ws, [lay.cash_last + 1], right) == []                              # one empty row before S8
    assert ws[f"J{lay.sum_usd}"].value == "Summary (USD)" and lay.sum_usd == lay.cash_last + 2
    assert blank(ws, [lay.sum_usd + 1], right) == []                                # one empty row before S9
    assert ws[f"J{lay.sum_base}"].value == "Summary (HUF)" and lay.sum_base == lay.sum_usd + 2
    assert blank(ws, [lay.sum_base + 1], right) == []


def test_section_headers_are_exactly_as_specified_and_bold(ws, lay):
    expected = {
        lay.t_hdr: dict(A="Name", B="Ticker", C="Quantity", D="Price", E="Total", F="Fee", G="Currency", H="Date",
                        J="Gross", K="Current Price", L="Current Total", M="USD Total", N="USD Gross",
                        O="USD Current Total", P="VUAA Quantity"),
        lay.d_hdr: dict(A="Name", B="Ticker", C="Quantity", D="Dividend", E="Total", F="Withholding Tax", G="Currency",
                        H="Date"),
        lay.p_hdr: dict(A="Description", B="Amount", C="USD.HUF", D="USD Amount", E="Date"),
        lay.f_hdr: dict(A="Currency Bought", B="Quantity", C="Price", D="Total", E="Fee", F="Currency Paid", G="Date"),
        lay.pos_hdr: dict(J="Ticker", K="Quantity", L="Current Price", M="Value", N="Return", O="USD Value",
                          P="USD Return", Q="CAGR"),
    }
    for row, cols in expected.items():
        for col, text in cols.items():
            c = ws[f"{col}{row}"]
            assert c.value == text, (col, row)
            assert c.font.bold and c.border.left.style == "thin" and c.border.bottom.style == "thin"
            assert c.fill.fgColor.rgb not in (None, "00000000")


def test_stock_rows_are_chronological_with_statement_data(ws, lay):
    dates = [ws[f"H{r}"].value for r in range(lay.t_first, lay.t_last + 1)]
    assert dates == sorted(dates) and isinstance(dates[0], datetime) and dates[0] == datetime(2026, 1, 29, 11, 12, 34)
    first = [ws[f"{c}{lay.t_first}"].value for c in "ABCDFG"]
    assert first == ["MICROSOFT CORP", "MSFT", 0.5, 422.0, 0.35035725, "USD"]
    assert all(ws[f"F{r}"].value > 0 for r in range(lay.t_first, lay.t_last + 1))        # fee = positive cost
    assert ws[f"H{lay.t_first}"].number_format == "yyyy-mm-dd hh:mm:ss"


def test_dividends_and_deposits_rows(ws, lay):
    divs = [[ws[f"{c}{r}"].value for c in "BCDEFG"] for r in range(lay.d_first, lay.d_last + 1)]
    assert len(divs) == 10 and all(d[1] > 0 and d[3] > 0 and d[4] >= 0 and d[5] == "USD" for d in divs)
    assert [ws[f"H{r}"].value for r in range(lay.d_first, lay.d_last + 1)] == sorted(
        ws[f"H{r}"].value for r in range(lay.d_first, lay.d_last + 1))
    assert ws[f"A{lay.p_first}"].value == "deposit" and ws[f"B{lay.p_first}"].value == 200000.0
    assert ws[f"F{lay.p_first}"].value == "HUF"
    assert ws[f"A{lay.p_first}"].comment and "Internal Transfer" in ws[f"A{lay.p_first}"].comment.text
    fx = [ws[f"{c}{lay.f_first}"].value for c in "ABCEFG"]
    assert fx[0] in ("USD", "HUF") and ws[f"F{lay.f_first}"].value in ("USD", "HUF")


# ======================================================================================================
# formulas, not values
# ======================================================================================================
def test_calculated_columns_are_formulas(ws, lay):
    def is_f(ref):
        v = ws[ref].value
        return isinstance(v, str) and v.startswith("=")

    for r in range(lay.t_first, lay.t_last + 1):
        for col in "EJKLMNOPRS":
            assert is_f(f"{col}{r}"), f"{col}{r}"
        assert not is_f(f"C{r}") and not is_f(f"D{r}") and not is_f(f"F{r}")          # statement records stay values
    for r in range(lay.p_first, lay.p_last + 1):
        assert all(is_f(f"{c}{r}") for c in "CDG")
    for r in range(lay.f_first, lay.f_last + 1):
        assert is_f(f"D{r}")
    for r in range(lay.pos_first, lay.pos_last + 1):
        assert all(is_f(f"{c}{r}") for c in "LMNOPST") and ws[f"Q{r}"].value is None     # CAGR column: empty by design
        assert not is_f(f"K{r}") and ws[f"K{r}"].font.color.rgb.endswith("0000FF")       # share count = statement value
    for r in range(lay.cash_first, lay.cash_last + 1):
        assert is_f(f"K{r}") and is_f(f"O{r}")
    assert all(is_f(f"{c}{lay.sum_usd}") for c in "OPQR")
    assert all(is_f(f"{c}{lay.sum_base}") for c in "MNQR")
    assert is_f(f"K{lay.bm_hdr + 1}")


def test_key_formulas_follow_the_specification(ws, lay):
    r = lay.t_first
    assert ws[f"E{r}"].value == f"=C{r}*D{r}" and ws[f"J{r}"].value == f"=E{r}+F{r}"
    assert ws[f"L{r}"].value == f"=C{r}*K{r}*SUMIFS(Pos_Factor,Pos_Ticker,B{r},Pos_Ccy,G{r})"
    assert ws[f"M{r}"].value == f"=E{r}/R{r}"
    assert ws[f"N{r}"].value == f"=J{r}/R{r}" and ws[f"O{r}"].value == f"=L{r}/R{r}"
    assert ws[f"P{r}"].value == f'=IF(N(S{r})>0,M{r}/S{r},"")'
    p = lay.p_first
    assert ws[f"D{p}"].value.startswith(f'=IF(F{p}="USD",B{p},IF(F{p}=BaseCcy,B{p}/C{p},')     # division by USD.HUF
    s, h = lay.sum_usd, lay.sum_base
    assert ws[f"O{s}"].value == f"=SUM(O{lay.pos_first}:O{lay.cash_last})"
    assert ws[f"P{s}"].value == f'=IFERROR(O{s}/SUMIFS(Dep_USDAmt,Dep_Desc,"<>interest"),"")'
    assert ws[f"Q{s}"].value == f'=IFERROR(P{s}^(1/R{s})-1,"")'
    assert ws[f"M{h}"].value == f"=O{s}*INDEX(FXL_Used,MATCH(BaseCcy,FXL_Ccy,0))"
    assert ws[f"N{h}"].value == f'=IFERROR(M{h}/SUMIFS(Dep_BaseAmt,Dep_Desc,"<>interest"),"")'
    assert ws[f"Q{h}"].value == f'=IFERROR(N{h}^(1/R{s})-1,"")'
    pos = lay.pos_first
    assert ws[f"N{pos}"].value.startswith(f'=IF(K{pos}=0,"",IFERROR(M{pos}/SUMIFS(Trade_Gross,')
    assert ws[f"S{pos}"].value == f"=SUMIFS(Trade_Qty,Trade_Ticker,$J{pos},Trade_Ccy,$R{pos})"
    assert ws[f"T{pos}"].value == f"=IF(ABS(S{pos})<0.000001,1,K{pos}/S{pos})"
    b = lay.bm_hdr
    assert ws[f"J{b + 11}"].value == "VUAA CAGR (USD)"
    assert ws[f"K{b + 11}"].value == f'=IFERROR(IF(K{b + 10}>0,K{b + 7}^(1/K{b + 10})-1,"n/a"),"n/a")'
    assert ws[f"K{b + 10}"].value == (f'=IFERROR(SUMPRODUCT(Trade_USDTotal*(AsOfDate-INT(Trade_Date)))/K{b + 4}'
                                      f'/DaysPerYear,"n/a")')


def test_only_widely_supported_functions(wb):
    allowed = {"SUM", "SUMIFS", "SUMPRODUCT", "INDEX", "MATCH", "IF", "IFERROR", "ABS", "INT", "N", "COUNTIF"}
    analysis = allowed | {"COUNTIFS", "SUMIF", "MAX", "LARGE", "FIXED", "ROUND"}                    # the Allocation sheet (see test_excel_analysis)
    used, used_analysis = set(), set()
    for sheet, _, f in formulas(wb):
        found = {t.value[:-1].upper() for t in Tokenizer(f).items if t.type == "FUNC" and t.subtype == "OPEN"}
        (used_analysis if sheet in ("Allocation", "Risk", "RiskData") else used).update(found)
    assert used and used <= allowed, sorted(used - allowed)                       # no XLOOKUP / FILTER / LET / dynamic arrays
    assert used_analysis <= analysis, sorted(used_analysis - analysis)


def test_every_name_used_in_a_formula_is_defined(wb):
    cell = re.compile(r"^(?:'?[A-Za-z_ ]+'?!)?\$?[A-Z]{1,3}\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?$")
    unknown = set()
    for sheet, coord, f in formulas(wb):
        for t in Tokenizer(f).items:
            if t.type == "OPERAND" and t.subtype == "RANGE" and not cell.match(t.value):
                if t.value not in wb.defined_names:
                    unknown.add((sheet, coord, t.value))
    assert not unknown, sorted(unknown)[:5]


def test_named_ranges(wb, lay):
    names = {n: d.attr_text for n, d in wb.defined_names.items()}
    assert names["Trade_Qty"] == "Portfolio!$C$2:$C$23" and names["Trade_Gross"] == "Portfolio!$J$2:$J$23"
    assert names["Trade_VUAAQty"] == "Portfolio!$P$2:$P$23"
    assert names["Dep_USDAmt"] == "Portfolio!$D$46:$D$59" and names["Dep_Date"] == "Portfolio!$E$46:$E$59"
    assert names["Div_Total"] == "Portfolio!$E$30:$E$39" and names["FXT_Qty"] == "Portfolio!$B$66:$B$127"
    assert names["BaseCcy"] == "Prices!$B$5" and names["AsOfDate"] == "Prices!$B$6" and names["DaysPerYear"] == "Prices!$B$7"
    assert names["BenchPriceUSD"] == "Prices!$F$14"
    assert names["Px_Key"] == "Prices!$A$17:$A$22" and names["Px_Used"] == "Prices!$I$17:$I$22"
    pf, pl = lay.pos_first, lay.pos_last
    assert names["Pos_Ticker"] == f"Portfolio!$J${pf}:$J${pl}" and names["Pos_Ccy"] == f"Portfolio!$R${pf}:$R${pl}"
    assert names["Pos_TxQty"] == f"Portfolio!$S${pf}:$S${pl}" and names["Pos_Factor"] == f"Portfolio!$T${pf}:$T${pl}"
    n_fx = 1 + sum(1 for r in range(2, wb["FX"].max_row + 1) if wb["FX"].cell(r, 1).value)
    assert names["FX_Dates"] == f"FX!$A$2:$A${n_fx}" and names["FX_Hdr"].startswith("FX!$B$1:$")
    assert wb["FX"]["A1"].value == "Date" and wb["FX"]["B1"].value == "USD" and wb["FX"]["C1"].value == "HUF"


def test_colour_code(ws, lay):
    r = lay.t_first
    assert ws[f"A{r}"].font.color.rgb.endswith("0000FF") and ws[f"C{r}"].font.color.rgb.endswith("0000FF")   # data = blue
    assert ws[f"E{r}"].font.color is None or ws[f"E{r}"].font.color.rgb in (None, "FF000000")               # formula = black
    assert ws[f"K{r}"].font.color.rgb.endswith("008000")                                                     # lookup = green
    assert ws[f"R{r}"].fill.fgColor.rgb.endswith("EDEDED")                                                   # helper column
    assert ws[f"A{r}"].font.name == "Arial"


def test_prices_sheet_layout_and_override_cells(wb):
    px = wb["Prices"]
    assert px["B5"].value == "HUF" and px["B7"].value == 365.0 and px["A5"].value == "Base currency"
    assert [px[f"B{r}"].value for r in range(17, 23)] == ["AMZN", "BN", "GOOGL", "META", "MSFT", "NVO"]
    assert px["E17"].value == 248.0 and px["G17"].value.startswith("IBKR statement")        # statement fallback
    assert px["I17"].value == '=IF(H17<>"",H17,E17)' and px["H17"].fill.fgColor.rgb.endswith("FFF2CC")      # yellow input
    assert [px[f"K{r}"].value for r in range(17, 20)] == ["USD", "HUF", "EUR"]
    assert px["P18"].value == '=IF(O18<>"",O18,L18)' and px["L18"].value == pytest.approx(327.38)
    assert px["F14"].value == '=IF(E14<>"",E14,B14)' and px["B14"].value is None            # no benchmark offline


def test_state_sheet_describes_the_archive(wb, pf_csv):
    st = wb["_state"]
    assert st["A1"].value == "layout" and st["B1"].value == WORKBOOK_VERSION == "ibkr-portfolio-tracker/layout-2"
    assert st["B2"].value == pf_csv.signature() and st["B3"].value == "U24013787"
    assert wb["Prices"]["B10"].value == pf_csv.signature()


def test_about_sheet_links_are_internal(wb):
    about = wb["About"]
    links = [c.hyperlink for row in about.iter_rows() for c in row if c.hyperlink]
    assert len(links) == 10 and all(h.location.startswith("Portfolio!") and not h.target for h in links)


# ======================================================================================================
# recalculation by LibreOffice == Python oracle
# ======================================================================================================
@pytest.fixture(scope="module")
def recalculated_offline(pf_csv, md_off, tmp_path_factory):
    path = tmp_path_factory.mktemp("xl_calc") / "offline.xlsx"
    build_workbook(pf_csv, md_off, SETTINGS).save(path)
    res = embed_values(path)
    return path, res


@pytest.fixture(scope="module")
def recalculated_online(pf_csv, md_on, tmp_path_factory):
    path = tmp_path_factory.mktemp("xl_calc") / "online.xlsx"
    build_workbook(pf_csv, md_on, dict(DEFAULT_SETTINGS)).save(path)
    res = embed_values(path)
    return path, res


@needs_lo
def test_offline_workbook_has_no_errors_and_matches_the_oracle(pf_csv, md_off, recalculated_offline, lay):
    path, res = recalculated_offline
    assert res.ok and res.errors == 0 and res.formulas > 400, res.message
    assert error_cells(path) == []
    vals = sheet_values(path, "Portfolio")
    assert compare_portfolio_sheet(vals, pf_csv, compute_metrics(pf_csv, md_off)) == []
    # the benchmark needs prices that an offline build does not have
    assert vals[f"K{lay.bm_hdr + 7}"] == "n/a" and all(f"P{r}" not in vals for r in range(lay.t_first, lay.t_last + 1))


@needs_lo
def test_regression_snapshot_of_the_sample_summary(recalculated_offline, lay):
    vals = sheet_values(recalculated_offline[0], "Portfolio")
    cash = {vals[f"J{r}"]: vals[f"K{r}"] for r in range(lay.cash_first, lay.cash_last + 1)}
    assert cash["HUF"] == pytest.approx(10633.268914319, abs=1e-6)                     # IBKR "Ending Cash"
    assert cash["USD"] == pytest.approx(0, abs=1e-6) and cash["EUR"] == 0
    s, h = lay.sum_usd, lay.sum_base
    assert vals[f"O{s}"] == pytest.approx(9641.1039, abs=1e-3)
    assert vals[f"P{s}"] == pytest.approx(1.0901617, abs=1e-6) and vals[f"R{s}"] == pytest.approx(0.4559213, abs=1e-6)
    assert vals[f"Q{s}"] == pytest.approx(0.2084567, abs=1e-6)
    assert vals[f"M{h}"] == pytest.approx(3156304.594, abs=0.01) and vals[f"N{h}"] == pytest.approx(1.1272516, abs=1e-6)
    assert vals[f"Q{h}"] == pytest.approx(0.3004707, abs=1e-6)
    assert vals[f"M{h}"] == pytest.approx(3156306.558, abs=5)                          # IBKR's NAV (rounded positions)


@needs_lo
def test_checks_sheet_agrees_with_the_statement(recalculated_offline, pf_csv):
    checks = sheet_values(recalculated_offline[0], "Checks")
    assert checks["B4"] == "all checks passed"
    rows = [r for r in range(6, 30) if f"A{r}" in checks]
    kinds = [checks[f"A{r}"] for r in rows]
    assert kinds.count("Cash balance") == 2 and kinds.count(SHARE_CHECK) == 6 and len(rows) == 9
    assert {checks[f"G{r}"] for r in rows} == {"OK"}
    cash_rows = {checks[f"B{r}"]: r for r in rows if checks[f"A{r}"] == "Cash balance"}
    assert checks[f"C{cash_rows['HUF']}"] == pytest.approx(10633.268914319) and checks[f"E{cash_rows['HUF']}"] == pytest.approx(0, abs=1e-6)
    nav = [r for r in rows if checks[f"A{r}"].startswith("Net asset value")][0]
    assert checks[f"C{nav}"] == pytest.approx(3156306.558314319) and abs(checks[f"E{nav}"]) < 5


@needs_lo
def test_online_workbook_matches_the_oracle_including_the_benchmark(pf_csv, md_on, recalculated_online, lay):
    path, res = recalculated_online
    assert res.ok and res.errors == 0, res.message
    assert error_cells(path) == []
    vals = sheet_values(path, "Portfolio")
    m = compute_metrics(pf_csv, md_on)
    assert m.benchmark.available and compare_portfolio_sheet(vals, pf_csv, m) == []
    assert all(isinstance(vals[f"P{r}"], float) for r in range(lay.t_first, lay.t_last + 1))        # VUAA quantities
    b = lay.bm_hdr
    assert vals[f"K{b + 3}"] == pytest.approx(vals[f"K{b + 1}"] * vals[f"K{b + 2}"])                 # units x price
    assert vals[f"K{b + 9}"] == pytest.approx(vals[f"K{b + 6}"] - vals[f"K{b + 3}"])                 # stocks - VUAA
    px = sheet_values(path, "Prices")
    assert px["B6"] == datetime(2026, 10, 30) and px["B8"].startswith("Yahoo Finance")
    assert px["G17"] == "Yahoo Finance" and px["B14"] == pytest.approx(md_on.benchmark.latest.price)


@needs_lo
def test_embedded_values_keep_the_workbook_intact(pf_csv, recalculated_offline, lay):
    path, _ = recalculated_offline
    plain = load_workbook(path)
    cached = load_workbook(path, data_only=True)
    r = lay.t_first
    assert plain["Portfolio"][f"E{r}"].value == f"=C{r}*D{r}" and cached["Portfolio"][f"E{r}"].value == pytest.approx(211.0)
    assert plain.calculation.fullCalcOnLoad is True                                   # Excel still recalculates on open
    assert plain.sheetnames == cached.sheetnames and plain["_state"].sheet_state == "hidden"
    assert {n for n in plain.defined_names} >= {"Trade_Qty", "BaseCcy", "FX_Table", "Px_Used"}
    links = [c.hyperlink for row in plain["About"].iter_rows() for c in row if c.hyperlink]
    assert len(links) == 10 and all(h.location.startswith("Portfolio!") and not h.target for h in links)  # stay internal
    assert plain["Portfolio"][f"A{r}"].font.color.rgb.endswith("0000FF")             # styles untouched
    assert plain["Portfolio"]["A1"].comment is not None
