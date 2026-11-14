"""The day-by-day history in the exports: the Excel *History* sheet (values + charts, rewritten at every refresh, added
to workbooks of earlier versions on their next refresh) and ``history.csv``."""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime

import openpyxl
import pandas as pd
import pytest

from conftest import SAMPLE_CSV, TODAY, needs_lo
from fakes import FakeProvider
from helpers import error_cells, layout_for, sheet_values
from mdfactory import END, daily, market, px_sap
from synth import ACCOUNT, make_csv
from ibkr_tracker import tables as T
from ibkr_tracker.calc import compute_metrics
from ibkr_tracker.config import DEFAULT_SETTINGS
from ibkr_tracker.excel import build_workbook
from ibkr_tracker.excel import layout as L
from ibkr_tracker.exports import csv_files, csv_tables, csv_zip, history_csv
from ibkr_tracker.history import build_history
from ibkr_tracker.parsing import parse_statement
from ibkr_tracker.portfolio import merge_statements
from ibkr_tracker.storage import Store
from ibkr_tracker.tracker import Tracker

SETTINGS = dict(DEFAULT_SETTINGS, recalc_with_libreoffice=False)
D = date


def roundtrip(wb) -> openpyxl.Workbook:
    """What a user opening the saved file gets (charts and values as they were written)."""
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return openpyxl.load_workbook(buf)


def table_rows(ws, ncols: int) -> list[list]:
    return [[c.value for c in row[:ncols]] for row in ws.iter_rows(min_row=L.HIST_HDR, max_row=ws.max_row)]


# ======================================================================================================
# the sheet
# ======================================================================================================
@pytest.fixture(scope="module")
def built(pf_synth):
    md = market(pf_synth)
    wb = build_workbook(pf_synth, md, SETTINGS, generated_at=datetime(2026, 11, 2, 12, 0))
    return wb, build_history(pf_synth, md), md


def test_the_workbook_has_a_history_sheet_next_to_the_market_data(built):
    wb, _, _ = built
    assert wb.sheetnames == ["Portfolio", "Prices", "FX", "Benchmark", "History", "Checks", "About", "_state"]


def test_every_day_and_every_column_of_the_reconstruction_is_on_the_sheet(built):
    wb, h, _ = built
    ws = roundtrip(wb)["History"]
    df = T.history_table(h)
    rows = table_rows(ws, df.shape[1])
    assert rows[0] == list(df.columns)                                              # the header
    assert len(rows) - 1 == h.days == 356 and rows[1][0] == datetime(2025, 1, 10) and rows[-1][0] == datetime(2025, 12, 31)
    for i, (got, want) in enumerate(zip(rows[1:], df.itertuples(index=False))):
        assert got[0] == datetime.combine(want[0], datetime.min.time()), i
        for j, (a, b) in enumerate(zip(got[1:], want[1:]), start=1):
            if b is None or (isinstance(b, float) and b != b):                      # no value (young CAGR, no benchmark)
                assert a is None, (i, df.columns[j], a)
            else:
                assert a == pytest.approx(b, rel=1e-12, abs=1e-9), (i, df.columns[j])


def test_the_last_row_is_the_current_valuation(built, pf_synth):
    wb, h, md = built
    ws = roundtrip(wb)["History"]
    m = compute_metrics(pf_synth, md)
    head = [c.value for c in ws[L.HIST_HDR]]
    last = dict(zip(head, [c.value for c in ws[ws.max_row]]))
    assert last["Total value (USD)"] == pytest.approx(m.usd_total) and last["Total value (EUR)"] == pytest.approx(m.base_total)
    assert last["Net deposits (EUR)"] == pytest.approx(m.base_deposits) and last["CAGR (EUR)"] == pytest.approx(m.base_cagr)
    assert last["Return multiple (USD)"] == pytest.approx(m.usd_return)
    assert last["AAPL (USD)"] == pytest.approx(6 * md.price("AAPL", "USD"))


def test_number_formats_and_the_title_block(built):
    wb, h, _ = built
    ws = wb["History"]
    assert ws["A1"].value.startswith("Portfolio history") and "356 days" in ws["A2"].value
    assert "2025-01-10 to 2025-12-31" in ws["A2"].value and "not formulas" in ws["A2"].value
    r = L.HIST_HDR + 2
    assert ws[f"A{r}"].number_format == "yyyy-mm-dd" or "yy" in ws[f"A{r}"].number_format
    head = {c.value: c.column_letter for c in ws[L.HIST_HDR] if c.value}
    assert "%" in ws[f"{head['CAGR (USD)']}{ws.max_row}"].number_format
    assert ws[f"{head['Total value (USD)']}{r}"].number_format.startswith("#,##0")
    assert ws.auto_filter.ref == f"A{L.HIST_HDR}:{openpyxl.utils.get_column_letter(len(head))}{ws.max_row}"


def test_two_charts_of_the_whole_lifespan(built):
    wb, h, _ = built
    ws = roundtrip(wb)["History"]
    head = {c.value: c.column_letter for c in ws[L.HIST_HDR] if c.value}
    first, last = L.HIST_HDR + 1, L.HIST_HDR + h.days

    def rng(name):
        return f"'History'!${head[name]}${first}:${head[name]}${last}"

    assert len(ws._charts) == 2
    titles = [ch.title.tx.rich.p[0].r[0].t for ch in ws._charts]
    assert titles[0] == "Portfolio value and net deposits (EUR)"
    assert titles[1] == "Stocks vs. VUAA.L replay of the purchases (USD)"
    assert [s.val.numRef.f for s in ws._charts[0].series] == [rng("Total value (EUR)"), rng("Net deposits (EUR)")]
    assert [s.val.numRef.f for s in ws._charts[1].series] == [rng("Stocks (USD)"), rng("Benchmark value (USD)")]
    for ch in ws._charts:
        assert all(s.cat.numRef.f == rng("Date") for s in ch.series)                        # the dates of every day


def test_without_benchmark_prices_the_second_chart_shows_the_return_multiple(pf_synth):
    md = market(pf_synth, bench=None)
    ws = roundtrip(build_workbook(pf_synth, md, SETTINGS))["History"]
    assert len(ws._charts) == 2
    assert ws._charts[1].title.tx.rich.p[0].r[0].t == "Return multiple (EUR): value / net deposits"
    head = [c.value for c in ws[L.HIST_HDR]]
    col = head.index("Benchmark value (USD)")
    assert all(ws.cell(r, col + 1).value is None for r in range(L.HIST_HDR + 1, ws.max_row + 1))


def test_the_coverage_notes_are_on_the_about_sheet(pf_synth):
    md = market(pf_synth, prices={"SAP|EUR": daily(D(2025, 4, 1), END, px_sap, True)})          # SAP: no price before April
    wb = roundtrip(build_workbook(pf_synth, md, SETTINGS))
    assert "1 data-coverage note(s)" in wb["History"]["A2"].value
    about = " ".join(str(c.value) for row in wb["About"].iter_rows() for c in row if c.value)
    assert "SAP (EUR)" in about and "last purchase / sale price" in about


def test_the_sheet_of_an_account_without_any_history_says_so(pf_synth):
    """(Not reachable with an imported statement - its period always gives a first day - but the branch must not crash.)"""
    from ibkr_tracker.excel.builder import _write_history
    from ibkr_tracker.history import empty_history
    md = market(pf_synth)
    ws = openpyxl.Workbook().active
    h = empty_history(pf_synth, md)
    assert h.empty and h.days == 0
    _write_history(ws, pf_synth, md, h)
    assert ws["A1"].value.startswith("Portfolio history") and ws["A3"].value.startswith("No history")
    assert len(ws._charts) == 0 and ws.max_row == 3


def test_an_account_without_transactions_still_gets_its_lifespan_from_the_statement_period():
    data, _ = make_csv(only_from=D(2026, 1, 1))                                               # only the Open Positions
    pf = merge_statements([parse_statement("s.csv", data)])
    ws = roundtrip(build_workbook(pf, market(pf), SETTINGS))["History"]
    head = [c.value for c in ws[L.HIST_HDR] if c.value]
    assert ws.max_row == L.HIST_HDR + 365 and ws.cell(L.HIST_HDR + 1, 1).value == datetime(2025, 1, 1)
    assert ws.cell(ws.max_row, head.index("Total value (USD)") + 1).value > 0


def test_a_usd_account_has_no_duplicate_columns_and_charts_of_its_own_columns():
    from synth import USD_ACCOUNT, make_usd_csv
    pf = merge_statements([parse_statement("u.csv", make_usd_csv()[0])], USD_ACCOUNT)
    prices = {f"{t}|{c}": daily(D(2024, 12, 30), END, lambda d: 100.0, True) for t, c in pf.positions_keys()}
    from mdfactory import make_md
    md = make_md(pf, prices=prices, fx={}, bench=daily(D(2024, 12, 30), END, lambda d: 100.0, True), as_of=END)
    ws = roundtrip(build_workbook(pf, md, SETTINGS))["History"]
    head = [c.value for c in ws[L.HIST_HDR] if c.value]
    assert len(head) == len(set(head)) and not any("USD per USD" in x for x in head)
    assert head[:4] == ["Date", "Stocks (USD)", "Cash (USD)", "Total value (USD)"]
    assert len(ws._charts) == 2 and ws._charts[0].title.tx.rich.p[0].r[0].t == "Portfolio value and net deposits (USD)"


# ======================================================================================================
# kept up to date by Refresh Prices - and added to workbooks of earlier versions
# ======================================================================================================
@pytest.fixture
def tr(home):
    t = Tracker(Store(home / "t.db"), FakeProvider(last_day=TODAY), today=lambda: TODAY)
    t.update_settings(export_excel=True, recalc_with_libreoffice=False)
    assert t.import_statement("synthetic.csv", make_csv()[0]).ok
    return t


def history_of(path):
    ws = openpyxl.load_workbook(path)["History"]
    head = [c.value for c in ws[L.HIST_HDR] if c.value]
    return head, [[c.value for c in row[:len(head)]] for row in ws.iter_rows(min_row=L.HIST_HDR + 1, max_row=ws.max_row)]


def test_refresh_prices_rewrites_the_history_in_place(tr):
    out = tr.refresh_prices(ACCOUNT)
    assert out.ok and out.update.action == "created", out.errors
    path = tr.workbook_path(tr.portfolio(ACCOUNT))
    head, rows = history_of(path)
    h = out.snapshot.history()
    assert len(rows) == h.days and rows[-1][0] == datetime.combine(h.end, datetime.min.time())
    assert h.end == D(2026, 10, 30) and h.days == (h.end - h.start).days + 1             # up to the newest price
    tr.save_manual_prices("SAP|EUR", [(D(2026, 10, 29), 777.0), (D(2026, 10, 30), 777.0)])
    out = tr.refresh_prices(ACCOUNT)
    assert out.update.action == "updated", out.update.reason                              # the same workbook, updated
    head2, rows2 = history_of(path)
    assert head2 == head and len(rows2) == len(rows)
    col = head.index("SAP (USD)")
    fx = out.snapshot.md.fx_used("EUR")
    assert rows2[-1][col] == pytest.approx(5 * 777.0 / fx) and rows2[-1][col] != rows[-1][col]
    assert rows2[10] == rows[10]                                                          # early days: unchanged


def test_a_manual_price_changes_the_workbook_without_any_download(tr):
    tr.refresh_prices(ACCOUNT)
    path = tr.workbook_path(tr.portfolio(ACCOUNT))
    before = history_of(path)[1]
    tr.save_manual_prices("AAPL|USD", [(D(2025, 3, 5), 1000.0)])                          # a single day, in March
    out = tr.export(ACCOUNT)
    assert out.ok and out.update.action == "updated"
    head, after = history_of(path)
    day = [r[0] for r in after].index(datetime(2025, 3, 5))
    assert after[day][head.index("AAPL (USD)")] == pytest.approx(10 * 1000.0)
    assert after[day][head.index("AAPL (USD)")] != before[day][head.index("AAPL (USD)")]
    assert after[day + 5][head.index("AAPL (USD)")] == before[day + 5][head.index("AAPL (USD)")]


def test_a_workbook_from_before_the_history_sheet_gets_it_on_the_next_refresh(tr):
    tr.refresh_prices(ACCOUNT)
    path = tr.workbook_path(tr.portfolio(ACCOUNT))
    wb = openpyxl.load_workbook(path)
    del wb["History"]                                                                     # a workbook of version 1.2
    wb.save(path)
    assert "History" not in openpyxl.load_workbook(path).sheetnames
    status, _, _ = tr.workbook_status(tr.portfolio(ACCOUNT))
    assert status == "current"                                                            # still a valid workbook
    out = tr.refresh_prices(ACCOUNT)
    assert out.ok and out.update.action == "updated" and out.update.backup is None        # not rebuilt, nothing lost
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["Portfolio", "Prices", "FX", "Benchmark", "History", "Checks", "About", "_state"]
    assert len(history_of(path)[1]) == out.snapshot.history().days


def test_the_consolidated_workbook_has_the_history_of_all_accounts(tr):
    from synth import USD_ACCOUNT, make_usd_csv
    assert tr.import_statement("usd.csv", make_usd_csv()[0]).ok
    from ibkr_tracker.portfolio import CONSOLIDATED
    tr.update_settings(consolidated_base="USD")
    out = tr.refresh_prices(CONSOLIDATED)
    assert out.ok, out.errors
    snap = tr.snapshot(CONSOLIDATED)
    h = snap.history()
    head, rows = history_of(tr.workbook_path(tr.consolidated_portfolio()))
    assert len(rows) == h.days and h.start == min(tr.portfolio(ACCOUNT).first_activity(),
                                                  tr.portfolio(USD_ACCOUNT).first_activity())
    assert rows[-1][head.index("Total value (USD)")] == pytest.approx(snap.metrics.usd_total)


# ======================================================================================================
# against LibreOffice's own calculation of the formulas
# ======================================================================================================
@needs_lo
def test_the_last_day_of_the_history_equals_the_formula_totals_of_the_portfolio_sheet(home):
    t = Tracker(Store(home / "t.db"), FakeProvider(last_day=TODAY), today=lambda: TODAY)
    t.update_settings(export_excel=True, recalc_with_libreoffice=True)
    assert t.import_statement(SAMPLE_CSV.name, SAMPLE_CSV.read_bytes()).ok
    out = t.refresh_prices("U24013787")
    assert out.ok, out.errors
    path = t.workbook_path(t.portfolio("U24013787"))
    assert error_cells(path) == []
    lay = layout_for(t.portfolio("U24013787"))
    vals = sheet_values(path, "Portfolio")
    ws = openpyxl.load_workbook(path, data_only=True)["History"] if False else None
    wb = openpyxl.load_workbook(path, data_only=True)
    hs = wb["History"]
    head = [c.value for c in hs[L.HIST_HDR]]
    last = dict(zip(head, [c.value for c in hs[hs.max_row]]))
    assert last["Total value (USD)"] == pytest.approx(vals[f"O{lay.sum_usd}"])
    assert last["Total value (HUF)"] == pytest.approx(vals[f"M{lay.sum_base}"])
    assert last["Return multiple (USD)"] == pytest.approx(vals[f"P{lay.sum_usd}"])
    assert last["CAGR (USD)"] == pytest.approx(vals[f"Q{lay.sum_usd}"])
    assert len(hs._charts) in (0, 2)                                                       # (LibreOffice keeps or drops them)


# ======================================================================================================
# CSV
# ======================================================================================================
@pytest.fixture(scope="module")
def snap(pf_synth, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("csv") / "t.db")
    store.save_settings(dict(DEFAULT_SETTINGS))
    t = Tracker(store, FakeProvider(last_day=TODAY), today=lambda: TODAY)
    assert t.import_statement("synthetic.csv", make_csv()[0]).ok
    t.refresh_prices(ACCOUNT)
    return t, t.snapshot(ACCOUNT)


def test_history_csv_is_one_of_the_csv_files(snap):
    t, s = snap
    assert "history.csv" in csv_tables(s) and "history.csv" in csv_files(s) and "history.csv" in t.csv_files(s)
    with zipfile.ZipFile(io.BytesIO(csv_zip(s))) as z:
        assert "history.csv" in z.namelist()


def test_history_csv_has_one_row_per_day(snap):
    t, s = snap
    h = s.history()
    data = history_csv(s)
    assert data.startswith(b"\xef\xbb\xbf")                                               # UTF-8 with BOM, for Excel
    df = pd.read_csv(io.BytesIO(data), encoding="utf-8-sig")
    assert list(df.columns) == list(T.history_table(h).columns) and len(df) == h.days
    assert df["Date"].iloc[0] == str(h.start) and df["Date"].iloc[-1] == str(h.end)
    assert df["Total value (USD)"].iloc[-1] == pytest.approx(s.metrics.usd_total)
    assert df["Total value (EUR)"].iloc[-1] == pytest.approx(s.metrics.base_total)
    assert df["CAGR (USD)"].iloc[:30].isna().all() and df["CAGR (USD)"].iloc[-1] == pytest.approx(s.metrics.usd_cagr)


def test_history_csv_in_the_european_dialect(snap):
    t, s = snap
    text = history_csv(s, "european").decode("utf-8-sig")
    lines = text.splitlines()
    assert lines[0].startswith("Date;Stocks (USD);Cash (USD);Total value (USD)")
    df = pd.read_csv(io.StringIO(text), sep=";", decimal=",")
    assert len(df) == s.history().days
    assert df["Total value (USD)"].iloc[-1] == pytest.approx(s.metrics.usd_total)
    assert df.to_numpy().dtype != object or df["Total value (USD)"].dtype.kind == "f"


def test_no_history_csv_without_numbers(home):
    t = Tracker(Store(home / "t.db"), FakeProvider(last_day=TODAY), today=lambda: TODAY)
    t.update_settings(export_excel=False)
    assert t.import_statement("synthetic.csv", make_csv()[0]).ok
    s = t.snapshot(ACCOUNT, dict(t.settings(), price_source="statement"))
    assert history_csv(s) is not None                                                     # statement prices: it works
    s.metrics = None
    s._history = None
    assert history_csv(s) is None and csv_tables(s) == {}
