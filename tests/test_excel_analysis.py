"""The Allocation, Risk and RiskData sheets of the workbook: the formulas are recalculated by LibreOffice and compared with
the Python engine (:mod:`ibkr_tracker.allocation`, :mod:`ibkr_tracker.analytics`) cell by cell."""
from __future__ import annotations

import math
import zipfile
from datetime import date, datetime
from typing import Any, Optional

import pytest
from openpyxl import load_workbook
from openpyxl.chart import DoughnutChart, LineChart
from openpyxl.formula import Tokenizer
from openpyxl.utils import get_column_letter

from conftest import TODAY, needs_lo
from fakes import FakeProvider
from helpers import error_cells
from mdfactory import market
from synth import USD_ACCOUNT, make_usd_csv
from ibkr_tracker import allocation as A
from ibkr_tracker import analytics as AN
from ibkr_tracker.config import DEFAULT_SETTINGS
from ibkr_tracker.excel import analysis_sheets as AS
from ibkr_tracker.excel import build_workbook, embed_values, refresh_workbook
from ibkr_tracker.excel import layout as L
from ibkr_tracker.market import MarketDataService
from ibkr_tracker.portfolio import CONSOLIDATED
from ibkr_tracker.storage import Store

ACCOUNT = "U24013787"
SETTINGS = dict(DEFAULT_SETTINGS, recalc_with_libreoffice=False)
CLASSIC = {"SUM", "SUMIF", "SUMIFS", "SUMPRODUCT", "INDEX", "MATCH", "IF", "IFERROR", "ABS", "INT", "N", "COUNTIF", "COUNTIFS",
           "COUNT", "MAX", "MIN", "LARGE", "AVERAGE", "STDEV", "SQRT", "PERCENTILE", "SLOPE", "CORREL", "ISNUMBER", "AND",
           "ROW", "FIXED", "ROUND"}
GROUPING_BLOCKS = {"asset_class": 0, "stock_type": 1, "sector": 2, "currency": 3}


# ---------------------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------------------
def priced_export(tracker, risk_free: Optional[dict] = None, lookup: bool = True, account: str = ACCOUNT):
    """Refresh the prices with the fake Yahoo and let LibreOffice calculate the workbook: ``(snapshot, path)``."""
    tracker.update_settings(recalc_with_libreoffice=True)
    if lookup:
        tracker.lookup_profiles(account)
    for ccy, pct in (risk_free or {}).items():
        tracker.set_risk_free(ccy, pct)
    out = tracker.refresh_prices(account)
    assert out.ok and out.update is not None, out.errors
    assert out.update.recalc is not None and out.update.recalc.ok, out.update.recalc and out.update.recalc.message
    return tracker.snapshot(account), out.update.path


def close(got: Any, want: Any, rel: float = 1e-9, abs_: float = 1e-12) -> bool:
    if want is None:
        return got in (None, "", "n/a")
    if isinstance(want, date):
        return isinstance(got, datetime) and got.date() == want
    if isinstance(got, (str, type(None))):
        return False
    return math.isclose(float(got), float(want), rel_tol=rel, abs_tol=abs_)


def labels(ws, col: str = "A") -> dict[str, int]:
    return {c.value: c.row for c in ws[col] if isinstance(c.value, str)}


def expected_metrics(rep: AN.RiskReport) -> dict[str, tuple[Any, Any]]:
    """Label of the Risk sheet -> (portfolio, benchmark) as the engine computes it."""
    p, b, rel = rep.portfolio, rep.benchmark, rep.relative
    g = lambda name: getattr(b, name) if b is not None else None               # noqa: E731
    out = {"Time-weighted return": (p.cumulative, g("cumulative")), "Annualised return": (p.annualized, g("annualized")),
           "Annualised volatility": (p.volatility, g("volatility")),
           "Downside deviation (annualised)": (p.downside_deviation, g("downside_deviation")),
           "Maximum drawdown": (p.max_drawdown, g("max_drawdown")),
           "Maximum drawdown - peak day": (p.mdd_peak, None), "Maximum drawdown - trough day": (p.mdd_trough, None),
           "Current drawdown": (p.current_drawdown, g("current_drawdown")),
           "Daily Value at Risk (95 %)": (p.var95, g("var95")),
           "Daily expected shortfall (95 %)": (p.cvar95, g("cvar95")),
           "Best day": (p.best_day, g("best_day")), "Worst day": (p.worst_day, g("worst_day")),
           "Days with a gain": (p.positive_share, g("positive_share")), "Sharpe ratio": (p.sharpe, g("sharpe")),
           "Sortino ratio": (p.sortino, g("sortino")), "Calmar ratio": (p.calmar, g("calmar"))}
    if rep.relative is not None:
        out.update({"Beta": (rel.beta, 1.0), "Correlation": (rel.correlation, 1.0),
                    "Alpha (Jensen, annualised)": (rel.alpha, 0.0), "Tracking error (annualised)": (rel.tracking_error, 0.0),
                    "Information ratio": (rel.information_ratio, None), "Treynor ratio": (rel.treynor, None)})
    return out


def check_risk_sheet(wb, snap, units: list[tuple[str, str, str]]) -> int:
    """Compare the Risk sheet with the engine for ``units`` = ``[(unit, portfolio column, benchmark column)]``; returns the
    number of cells compared."""
    ws = wb[L.RISK]
    rows = labels(ws)
    n = 0
    for unit, cp, cb in units:
        rep = snap.risk(unit)
        assert rep.ok, (unit, rep.status, rep.message)
        assert ws[f"{cp}5"].value == f"Portfolio ({rep.currency})"
        meta = {"Observation days": (rep.n_obs, rep.benchmark.n if rep.benchmark else None),
                "Years in the window": (rep.years, None), "Risk-free rate (annual)": (rep.risk_free, rep.risk_free),
                "Risk-free rate (daily)": ((1 + rep.risk_free) ** (1 / 252) - 1, (1 + rep.risk_free) ** (1 / 252) - 1),
                "Stock value with a recent price": (rep.price_coverage, None),
                "Window start (first transaction)": (rep.start, None), "Window end (valuation date)": (rep.end, None)}
        for label, (want_p, want_b) in {**meta, **expected_metrics(rep)}.items():
            row = rows[label]
            for col, want in ((cp, want_p), (cb, want_b)):
                got = ws[f"{col}{row}"].value
                assert close(got, want), f"{label} [{col}{row}]: workbook={got!r} python={want!r}"
                n += 1
        recovered = rep.portfolio.mdd_recovery
        got = ws[f"{cp}{rows['Maximum drawdown - back at the peak']}"].value
        assert (got == "not recovered yet") if recovered is None else (got.date() == recovered), got
    return n


def check_risk_data(wb, snap, unit: str, j: int) -> int:
    """The observation table of unit ``j`` on RiskData equals the engine's frame."""
    ws = wb[L.RISKDATA]
    rep = snap.risk(unit)
    f = rep.frame
    c0 = 1 + j * AS.BLOCK_W
    col = {name: c0 + i for i, name in enumerate(AS.BLOCK_COLS)}
    want = {"value": "value", "flow": "flow", "ret": "ret", "index": "index", "peak": "peak", "dd": "drawdown",
            "vol": "rolling_vol", "bpx": "bench_price", "bret": "bench_ret", "bidx": "bench_index",
            "bdd": "bench_drawdown"}
    n = 0
    for i, ts in enumerate(f.index):
        r = AS.DATA_FIRST + i
        assert ws.cell(r, col["date"]).value.date() == ts.date()
        for name, frame_col in want.items():
            exp = f[frame_col].iloc[i]
            exp = None if exp != exp else float(exp)
            got = ws.cell(r, col[name]).value
            assert close(got, exp, rel=1e-9, abs_=1e-12), f"{unit} row {r} {name}: workbook={got!r} python={exp!r}"
            n += 1
    assert ws.cell(AS.DATA_FIRST + len(f), col["date"]).value is None            # nothing below the last observation
    return n


def check_allocation_sheet(wb, snap) -> None:
    ws = wb[L.ALLOCATION]
    holdings = snap.holdings()
    for by, k in GROUPING_BLOCKS.items():
        s = AS.TOP + k * AS.SLOT
        allocation = snap.allocation(by)
        slices = A.chart_slices(allocation, AS.MAX_SLICES)
        assert ws[f"A{s}"].value == AS.BLOCKS[k][1]
        for i, sl in enumerate(slices):
            r = s + 2 + i
            assert ws[f"A{r}"].value == sl.label
            assert close(ws[f"B{r}"].value, sl.usd_value, abs_=1e-8), (by, sl.label, ws[f"B{r}"].value, sl.usd_value)
            assert close(ws[f"C{r}"].value, sl.weight) and ws[f"D{r}"].value == len(sl.members), (by, sl.label)
            assert ws[f"E{r}"].value == f"{sl.label}  ·  {sl.weight * 100:.1f}%", (by, ws[f"E{r}"].value)
        tot = s + 2 + len(slices)
        assert ws[f"A{tot}"].value == "Total" and close(ws[f"B{tot}"].value, allocation.total, abs_=1e-8)
        assert close(ws[f"C{tot}"].value, 1.0) and ws[f"E{tot}"].value in (None, "")        # adds up: no warning
    h0 = AS.TOP + len(AS.BLOCKS) * AS.SLOT + 11
    for i, h in enumerate(holdings):
        r = h0 + i
        assert (ws[f"A{r}"].value, ws[f"B{r}"].value) == (h.ticker, h.currency)
        assert close(ws[f"C{r}"].value, h.usd_value, abs_=1e-8)
        assert (ws[f"E{r}"].value, ws[f"F{r}"].value, ws[f"G{r}"].value) == (h.asset_class, h.stock_type, h.sector)
    total = h0 + len(holdings)
    assert ws[f"A{total}"].value == "Total (positive values)"
    assert close(ws[f"C{total}"].value, snap.allocation("asset_class").total, abs_=1e-8)
    assert close(ws[f"C{total + 1}"].value, snap.metrics.usd_total, abs_=1e-8)        # Section 8 of the Portfolio sheet
    assert abs(ws[f"C{total + 2}"].value) < 1e-6                                       # nothing left out in the sample
    conc = A.concentration(holdings)
    c = AS.TOP + len(AS.BLOCKS) * AS.SLOT
    assert ws[f"A{c + 2}"].value == "Stock positions" and ws[f"B{c + 2}"].value == conc.n_positions
    assert close(ws[f"B{c + 3}"].value, conc.largest_weight) and ws[f"C{c + 3}"].value == conc.largest_ticker
    assert close(ws[f"B{c + 4}"].value, conc.top3_weight) and close(ws[f"B{c + 5}"].value, conc.top5_weight)
    assert close(ws[f"B{c + 6}"].value, conc.effective_positions) and close(ws[f"B{c + 7}"].value, conc.cash_weight)


# ---------------------------------------------------------------------------------------------------------------
# the workbook of the sample account, prices refreshed
# ---------------------------------------------------------------------------------------------------------------
@needs_lo
def test_the_allocation_sheet_equals_the_engine(loaded):
    snap, path = priced_export(loaded)
    wb = load_workbook(path, data_only=True)
    check_allocation_sheet(wb, snap)
    assert wb.sheetnames[wb.sheetnames.index("History") + 1:][:3] == ["Allocation", "Risk", "RiskData"]
    assert error_cells(path) == []


@needs_lo
def test_the_risk_sheet_equals_the_engine_in_both_currencies(loaded):
    snap, path = priced_export(loaded)
    wb = load_workbook(path, data_only=True)
    n = check_risk_sheet(wb, snap, [("base", "B", "C"), ("usd", "D", "E")])
    assert n > 100
    assert check_risk_data(wb, snap, "base", 0) > 1500 and check_risk_data(wb, snap, "usd", 1) > 1500
    ws = wb[L.RISK]
    assert ws["A5"].value == "Metric" and ws["C5"].value == "VUAA (HUF)" and ws["E5"].value == "VUAA (USD)"
    assert ws["F5"].value == "What it means" and ws["A2"].value.startswith("Time-weighted daily returns")
    assert error_cells(path) == []


@needs_lo
def test_with_a_risk_free_rate_the_ratios_still_equal_the_engine(loaded):
    snap, path = priced_export(loaded, risk_free={"HUF": 6.5, "USD": 4.0})
    wb = load_workbook(path, data_only=True)
    assert snap.risk("base").risk_free == pytest.approx(0.065) and snap.risk("usd").risk_free == pytest.approx(0.04)
    check_risk_sheet(wb, snap, [("base", "B", "C"), ("usd", "D", "E")])
    rows = labels(wb[L.RISK])
    assert wb[L.RISK][f"B{rows['Risk-free rate (annual)']}"].value == pytest.approx(0.065)
    assert wb[L.RISK][f"D{rows['Risk-free rate (annual)']}"].value == pytest.approx(0.04)
    zero = snap.risk("usd", risk_free=0.0).portfolio
    assert snap.risk("usd").portfolio.sharpe < zero.sharpe                                 # the rate really matters


@needs_lo
def test_the_risk_free_rate_typed_into_the_workbook_flows_through(loaded, tmp_path):
    snap, path = priced_export(loaded)
    wb = load_workbook(path)                                                               # the formulas
    ws = wb[L.RISK]
    rows = labels(ws)
    ws[f"B{rows['Risk-free rate (annual)']}"] = 0.05
    ws[f"D{rows['Risk-free rate (annual)']}"] = 0.03
    edited = tmp_path / "edited.xlsx"
    wb.save(edited)
    assert embed_values(edited).ok
    values = load_workbook(edited, data_only=True)
    expected = {"base": snap.risk("base", risk_free=0.05), "usd": snap.risk("usd", risk_free=0.03)}
    for unit, col in (("base", "B"), ("usd", "D")):
        for label, getter in (("Sharpe ratio", lambda s: s.sharpe), ("Sortino ratio", lambda s: s.sortino),
                              ("Downside deviation (annualised)", lambda s: s.downside_deviation)):
            assert close(values[L.RISK][f"{col}{rows[label]}"].value, getter(expected[unit].portfolio)), (unit, label)
        rel = expected[unit].relative
        assert close(values[L.RISK][f"{col}{rows['Alpha (Jensen, annualised)']}"].value, rel.alpha)
        assert close(values[L.RISK][f"{col}{rows['Treynor ratio']}"].value, rel.treynor)


@needs_lo
def test_no_prices_no_risk_metrics_but_the_allocation(loaded):
    loaded.update_settings(recalc_with_libreoffice=True)
    out = loaded.export(ACCOUNT)                                                           # statement prices only
    assert out.ok and out.update.recalc.ok
    snap = loaded.snapshot(ACCOUNT)
    wb = load_workbook(out.update.path, data_only=True)
    ws = wb[L.RISK]
    assert snap.risk("usd").status == AN.NO_PRICES
    assert ws["A3"].value == snap.risk("base").message and ws["A3"].value.startswith("Daily prices are missing")
    assert ws["A5"].value is None and wb[L.RISKDATA]["A3"].value.startswith("No observation days")
    check_allocation_sheet(wb, snap)
    assert error_cells(out.update.path) == []
    assert snap.allocation("sector").get(A.UNCLASSIFIED) is not None                       # no lookup yet


@needs_lo
def test_the_sector_sheet_follows_a_manual_classification(loaded):
    loaded.update_settings(recalc_with_libreoffice=True)
    loaded.lookup_profiles(ACCOUNT)
    snap = loaded.snapshot(ACCOUNT)
    loaded.save_classification(snap.holdings(), {"NVO|USD": (A.STOCKS, "Biotech"), "META|USD": ("Other", "")})
    out = loaded.export(ACCOUNT)
    assert out.ok and out.update.action in ("updated", "rebuilt", "created")
    wb = load_workbook(out.update.path, data_only=True)
    snap = loaded.snapshot(ACCOUNT)
    check_allocation_sheet(wb, snap)
    sheet = wb[L.ALLOCATION]
    sector_labels = [sheet[f"A{AS.TOP + 2 * AS.SLOT + 2 + i}"].value for i in range(6)]
    assert "Biotech" in sector_labels and "Healthcare" not in sector_labels
    class_labels = [sheet[f"A{AS.TOP + 2}"].value, sheet[f"A{AS.TOP + 3}"].value, sheet[f"A{AS.TOP + 4}"].value]
    assert "Other" in class_labels


@needs_lo
def test_small_groups_are_merged_into_other(loaded, monkeypatch):
    monkeypatch.setattr(AS, "MAX_SLICES", 3)
    snap, path = priced_export(loaded)
    wb = load_workbook(path, data_only=True)
    ws = wb[L.ALLOCATION]
    s = AS.TOP + 2 * AS.SLOT                                                               # the sector block
    sectors = snap.allocation("sector")
    assert len(sectors.slices) == 6 and [ws[f"A{s + 2 + i}"].value for i in range(3)] == [
        sectors.slices[0].label, sectors.slices[1].label, "Other (4)"]
    assert close(ws[f"B{s + 4}"].value, sum(x.usd_value for x in sectors.slices[2:]), abs_=1e-8)
    assert ws[f"D{s + 4}"].value == sum(len(x.members) for x in sectors.slices[2:])
    assert close(ws[f"B{s + 5}"].value, sectors.total, abs_=1e-8) and ws[f"E{s + 5}"].value in (None, "")
    assert "merged into Other" in ws[f"A{s + 6}"].value
    check_risk_sheet(wb, snap, [("base", "B", "C"), ("usd", "D", "E")])


@needs_lo
def test_a_usd_account_has_a_single_unit(tracker):
    assert tracker.import_statement("usd.csv", make_usd_csv()[0]).ok
    tracker.update_settings(recalc_with_libreoffice=True)
    out = tracker.refresh_prices(USD_ACCOUNT)
    assert out.ok and out.update.recalc.ok, out.update.recalc.message
    snap = tracker.snapshot(USD_ACCOUNT)
    wb = load_workbook(out.update.path, data_only=True)
    ws = wb[L.RISK]
    assert [ws[f"{c}5"].value for c in "ABCF"] == ["Metric", "Portfolio (USD)", "VUAA (USD)", "What it means"]
    assert ws["D5"].value is None and ws["E5"].value is None                                 # one unit: no second pair of columns
    assert snap.risk("usd").ok
    check_risk_sheet(wb, snap, [("usd", "B", "C")])
    check_risk_data(wb, snap, "usd", 0)
    check_allocation_sheet(wb, snap)
    assert error_cells(out.update.path) == []


@needs_lo
def test_the_consolidated_workbook_has_the_analysis(tracker):
    from synth import make_csv
    for name, data in (("eur.csv", make_csv()[0]), ("usd.csv", make_usd_csv()[0])):
        assert tracker.import_statement(name, data).ok
    tracker.update_settings(recalc_with_libreoffice=True)
    tracker.lookup_profiles(CONSOLIDATED)
    out = tracker.refresh_prices(CONSOLIDATED)
    assert out.ok and out.update.recalc.ok, out.update.recalc.message
    snap = tracker.snapshot(CONSOLIDATED)
    wb = load_workbook(out.update.path, data_only=True)
    check_allocation_sheet(wb, snap)
    check_risk_sheet(wb, snap, [("base", "B", "C"), ("usd", "D", "E")] if snap.history().base_currency != "USD"
                     else [("usd", "B", "C")])
    assert error_cells(out.update.path) == []


# ---------------------------------------------------------------------------------------------------------------
# structure - no LibreOffice needed
# ---------------------------------------------------------------------------------------------------------------
@pytest.fixture
def built(pf_csv, tmp_path):
    store = Store(tmp_path / "t.db")
    svc = MarketDataService(store, FakeProvider(last_day=TODAY), today=lambda: TODAY)
    svc.refresh(pf_csv, SETTINGS)
    md = svc.build(pf_csv, SETTINGS)
    return pf_csv, md, build_workbook(pf_csv, md, SETTINGS, generated_at=datetime(2026, 11, 2, 12, 0))


def test_the_donut_charts_are_native_charts(built):
    pf, md, wb = built
    donuts = wb[L.ALLOCATION]._charts
    assert [type(c) for c in donuts] == [DoughnutChart] * 4 and {c.holeSize for c in donuts} == {58}
    assert [c.anchor for c in donuts] == [f"F{AS.TOP + k * AS.SLOT}" for k in range(4)]            # one per block, beside its table
    assert all(c.dataLabels is None for c in donuts)                                         # the weights are in the legend
    first = AS.TOP + 2                                                                       # the legend: "Group  ·  12.3%"
    assert donuts[0].series[0].cat.numRef.f == f"'Allocation'!$E${first}:$E${first + 1}"     # (stocks, cash)
    assert donuts[0].series[0].val.numRef.f == f"'Allocation'!$B${first}:$B${first + 1}"
    lines = wb[L.RISK]._charts
    assert [type(c) for c in lines] == [LineChart] * 4                                      # drawdown + rolling volatility x2
    assert len(wb[L.RISKDATA]._charts) == 0


def test_only_classic_functions_are_used(built):
    pf, md, wb = built
    used = set()
    for name in (L.ALLOCATION, L.RISK, L.RISKDATA):
        for row in wb[name].iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    used |= {t.value[:-1].upper() for t in Tokenizer(c.value).items
                             if t.type == "FUNC" and t.subtype == "OPEN"}
    assert used and used <= CLASSIC, sorted(used - CLASSIC)                                  # nothing needing _xlfn. (STDEV.S ...)


def test_the_charts_are_in_the_file(built, tmp_path):
    pf, md, wb = built
    path = tmp_path / "w.xlsx"
    wb.save(path)
    with zipfile.ZipFile(path) as z:
        charts = [n for n in z.namelist() if n.startswith("xl/charts/chart")]
    assert len(charts) == 2 + 4 + 4                                                          # History + Allocation + Risk


def test_the_about_sheet_describes_the_analysis_sheets(built):
    pf, md, wb = built
    about = wb[L.ABOUT]
    notes = {row[0].value: row[1].value for row in about.iter_rows(min_col=1, max_col=2) if row[0].value}
    assert "Donut charts" in notes["Allocation sheet"] and "RiskData" in notes["Risk sheets"]
    links = [c.hyperlink for row in about.iter_rows() for c in row if c.hyperlink]
    assert len(links) == 10 and all(h.location.startswith("Portfolio!") for h in links)         # the index is unchanged


def test_the_risk_sheet_formulas_read_the_observation_table(built):
    pf, md, wb = built
    ws = wb[L.RISK]
    rows = labels(ws)

    def rng(j: int, name: str) -> str:
        col = get_column_letter(1 + j * AS.BLOCK_W + AS.BLOCK_COLS.index(name))
        return f"RiskData!${col}${AS.DATA_FIRST}:${col}$"

    for j, col in ((0, "B"), (1, "D")):                                                      # HUF block, USD block
        vol = ws[f"{col}{rows['Annualised volatility']}"].value
        assert vol.startswith(f"=STDEV({rng(j, 'ret')}") and vol.endswith("*SQRT(252)")
        assert ws[f"{col}{rows['Sharpe ratio']}"].value.startswith(f"=IFERROR((AVERAGE({rng(j, 'ret')}")
        assert ws[f"{col}{rows['Maximum drawdown']}"].value.startswith(f"=MIN({rng(j, 'dd')}")
        beta = ws[f"{col}{rows['Beta']}"].value
        assert beta.startswith(f"=IFERROR(SLOPE({rng(j, 'pret_b')}") and f",{rng(j, 'bret')}" in beta
    ret = wb[L.RISKDATA][f"{get_column_letter(1 + AS.BLOCK_COLS.index('ret'))}{AS.DATA_FIRST + 1}"].value
    assert ret == f"=B{AS.DATA_FIRST + 1}/(B{AS.DATA_FIRST}+C{AS.DATA_FIRST + 1})-1"           # value / (previous + flow) - 1
    first = wb[L.RISKDATA][f"D{AS.DATA_FIRST}"].value
    assert first == f"=B{AS.DATA_FIRST}/C{AS.DATA_FIRST}-1"                                     # the first one starts from nothing


def test_the_analysis_sheets_are_added_to_an_older_workbook_on_refresh(built, tmp_path):
    pf, md, wb = built
    path = tmp_path / "old.xlsx"
    for name in (L.ALLOCATION, L.RISK, L.RISKDATA):                                          # a workbook of version 1.3
        del wb[name]
    wb.save(path)
    old = load_workbook(path)
    assert old.sheetnames == ["Portfolio", "Prices", "FX", "Benchmark", "History", "Checks", "About", "_state"]
    before = {c.coordinate: c.value for row in old[L.PORTFOLIO].iter_rows() for c in row if c.value is not None}
    update = refresh_workbook(path, pf, md, SETTINGS, recalc=False)
    assert update.action == "updated" and update.backup is None                              # not rebuilt, nothing lost
    new = load_workbook(path)
    assert new.sheetnames == ["Portfolio", "Prices", "FX", "Benchmark", "History", "Allocation", "Risk", "RiskData", "Checks",
                              "About", "_state"]
    after = {c.coordinate: c.value for row in new[L.PORTFOLIO].iter_rows() for c in row if c.value is not None}
    assert after == before                                                                   # the Portfolio sheet is untouched
    again = refresh_workbook(path, pf, md, SETTINGS, recalc=False)                           # and a second refresh is stable
    third = load_workbook(path)
    for name in (L.ALLOCATION, L.RISK, L.RISKDATA):
        a = {c.coordinate: c.value for row in new[name].iter_rows() for c in row if c.value is not None}
        b = {c.coordinate: c.value for row in third[name].iter_rows() for c in row if c.value is not None}
        assert a == b, name
    assert again.action == "updated"


def test_without_a_benchmark_the_risk_sheet_has_no_benchmark_columns(pf_synth):
    md = market(pf_synth, bench=None)
    wb = build_workbook(pf_synth, md, SETTINGS, generated_at=datetime(2026, 11, 2, 12, 0))
    ws = wb[L.RISK]
    rows = labels(ws)
    assert "Beta" not in rows and ws["B5"].value == "Portfolio (EUR)"
    assert ws[f"C{rows['Annualised volatility']}"].value == "n/a"
    assert ws[f"B{rows['Annualised volatility']}"].value.startswith("=STDEV(")
    assert len(ws._charts) == 4                                                              # drawdown + volatility, EUR and USD
    headers = [c.value for c in wb[L.RISKDATA][AS.DATA_HDR] if c.value]
    assert headers and not any("VUAA" in h for h in headers)


