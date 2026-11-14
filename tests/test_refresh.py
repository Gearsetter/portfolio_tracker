"""Refreshing the workbook on disk: in-place update vs rebuild, user overrides typed into Excel, locked files."""
from __future__ import annotations

import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from conftest import TODAY, needs_lo
from fakes import FakeProvider
from helpers import compare_portfolio_sheet, error_cells, sheet_values
from ibkr_tracker.calc import compute_metrics
from ibkr_tracker.config import DEFAULT_SETTINGS
from ibkr_tracker.excel import (WorkbookLocked, build_workbook, embed_values, open_current, refresh_workbook,
                                sync_overrides)
from ibkr_tracker.excel import overrides as OV
from ibkr_tracker.excel.recalc import inject_values
from ibkr_tracker.market import MarketDataService
from ibkr_tracker.storage import Store

OFFLINE = dict(DEFAULT_SETTINGS, price_source="statement", recalc_with_libreoffice=False)
ONLINE = dict(DEFAULT_SETTINGS, recalc_with_libreoffice=False)
STAMP = datetime(2026, 11, 2, 12, 0)


# ---- helpers ---------------------------------------------------------------------------------------------------
def fingerprint(ws, skip_empty=False):
    """Everything a user would call 'the sheet': values/formulas, formats, fonts, fills, borders, comments, widths.
    ``skip_empty`` ignores cells that hold neither a value nor a comment (used for sheets that are rewritten: their
    cleared cells may carry a plain style)."""
    cells = {}
    for row in ws.iter_rows():
        for c in row:
            if c.value is None and c.comment is None and (skip_empty or not c.has_style):
                continue
            f, fill = c.font, c.fill
            cells[c.coordinate] = (
                c.value, c.number_format, f.name, f.sz, f.b, f.i,
                f.color.rgb if f.color is not None and f.color.type == "rgb" else None,
                fill.fgColor.rgb if fill is not None and fill.fill_type else None,
                c.border.left.style if c.border.left else None,
                c.border.bottom.style if c.border.bottom else None, c.alignment.horizontal,
                c.comment.text if c.comment else None)
    widths = {k: v.width for k, v in ws.column_dimensions.items()}
    return cells, widths, ws.freeze_panes, len(ws.conditional_formatting)


def comment_widths(path: Path, sheet_index: int = 1) -> list[float]:
    """Widths (px) of the comment boxes of a sheet, read from the VML drawing in the file."""
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if re.match(r"xl/drawings/commentsDrawing\d+\.vml|xl/drawings/vmlDrawing\d+\.vml", n))
        text = z.read(names[sheet_index - 1]).decode("utf8")
    return [float(m) for m in re.findall(r"width:\s*([\d.]+)px", text)]


@pytest.fixture(scope="module")
def sample_services(pf_csv, tmp_path_factory):
    d = tmp_path_factory.mktemp("rf")
    off = MarketDataService(Store(d / "off.db"))
    on = MarketDataService(Store(d / "on.db"), FakeProvider(last_day=TODAY), today=lambda: TODAY)
    on.refresh(pf_csv, ONLINE)
    return off, on


@pytest.fixture
def md_off(pf_csv, sample_services):
    return sample_services[0].build(pf_csv, OFFLINE)


@pytest.fixture
def md_on(pf_csv, sample_services):
    return sample_services[1].build(pf_csv, ONLINE)


# ======================================================================================================
# create -> update in place
# ======================================================================================================
def test_first_call_creates_the_file(pf_csv, md_off, tmp_path):
    path = tmp_path / "out" / "wb.xlsx"
    u = refresh_workbook(path, pf_csv, md_off, OFFLINE, generated_at=STAMP, recalc=False)
    assert (u.action, u.reason, u.backup, u.recalc) == ("created", "no workbook yet", None, None) and path.exists()
    wb, why = open_current(path, pf_csv)
    assert wb is not None and why == ""
    assert sorted(p.name for p in path.parent.iterdir()) == ["wb.xlsx"]              # no temp or backup files left


def test_refresh_updates_in_place_and_leaves_portfolio_and_checks_untouched(pf_csv, md_off, md_on, tmp_path):
    path = tmp_path / "wb.xlsx"
    refresh_workbook(path, pf_csv, md_off, OFFLINE, generated_at=STAMP, recalc=False)
    wb1 = load_workbook(path)
    before = {n: fingerprint(wb1[n]) for n in ("Portfolio", "Checks")}
    prices_before = fingerprint(wb1["Prices"])
    widths_before = comment_widths(path, 1)

    u = refresh_workbook(path, pf_csv, md_on, ONLINE, generated_at=STAMP, recalc=False)
    assert u.action == "updated" and u.reason == "" and u.backup is None
    wb2 = load_workbook(path)
    assert {n: fingerprint(wb2[n]) for n in ("Portfolio", "Checks")} == before          # every formula, format, comment
    assert wb2.sheetnames == wb1.sheetnames and wb2["_state"].sheet_state == "hidden"
    assert fingerprint(wb2["Prices"]) != prices_before                                   # ... while the market data moved
    assert comment_widths(path, 1) and min(comment_widths(path, 1)) >= 250               # comment boxes keep a sane size
    assert len(widths_before) == len(comment_widths(path, 1))                            # none lost
    px = wb2["Prices"]
    assert px["B6"].value == datetime(2026, 10, 30) and px["B8"].value.startswith("Yahoo Finance")
    assert px["G17"].value == "Yahoo Finance" and px["E17"].value == pytest.approx(md_on.raw_prices["AMZN|USD"].price)
    assert px["B14"].value == pytest.approx(md_on.benchmark.latest.price) and px["C14"].value == datetime(2026, 10, 30)
    fx = wb2["FX"]
    dates = md_on.fx.union_dates()
    assert fx.cell(2, 1).value == datetime.combine(dates[0], datetime.min.time()) and fx.cell(1 + len(dates), 1).value
    assert fx.cell(2 + len(dates), 1).value is None and [fx.cell(1, c).value for c in (1, 2, 3)] == ["Date", "USD", "HUF"]
    assert wb2["Benchmark"]["A2"].value is not None and wb2["Benchmark"]["B2"].value > 0
    assert {n: d.attr_text for n, d in wb2.defined_names.items()}["BM_Dates"].startswith("Benchmark!$A$2:$A$")
    assert wb2["_state"]["B2"].value == pf_csv.signature()


def test_refresh_is_idempotent(pf_csv, md_on, tmp_path):
    path = tmp_path / "wb.xlsx"
    refresh_workbook(path, pf_csv, md_on, ONLINE, generated_at=STAMP, recalc=False)
    names = ("Portfolio", "Prices", "FX", "Benchmark", "Checks", "About")
    first = {n: fingerprint(load_workbook(path)[n], skip_empty=True) for n in names}
    u = refresh_workbook(path, pf_csv, md_on, ONLINE, generated_at=STAMP, recalc=False)
    assert u.action == "updated"
    assert {n: fingerprint(load_workbook(path)[n], skip_empty=True) for n in names} == first


@needs_lo
def test_recalculated_values_follow_the_refreshed_prices(pf_csv, md_off, md_on, tmp_path):
    path = tmp_path / "wb.xlsx"
    refresh_workbook(path, pf_csv, md_off, OFFLINE, recalc=True)
    off_vals = sheet_values(path, "Portfolio")
    assert off_vals["L30"] == 248.0                                                      # AMZN statement close
    u = refresh_workbook(path, pf_csv, md_on, ONLINE, recalc=True)
    assert u.action == "updated" and u.recalc.ok and u.recalc.errors == 0, u.recalc.message
    assert error_cells(path) == []
    vals = sheet_values(path, "Portfolio")
    assert vals["L30"] == pytest.approx(md_on.raw_prices["AMZN|USD"].price) != 248.0
    assert compare_portfolio_sheet(vals, pf_csv, compute_metrics(pf_csv, md_on)) == []
    assert sheet_values(path, "Checks")["B4"] in ("all checks passed",)


# ======================================================================================================
# rebuild
# ======================================================================================================
def test_rebuild_when_the_statement_archive_changes(pf_csv, pf_synth, md_off, tmp_path):
    path = tmp_path / "wb.xlsx"
    refresh_workbook(path, pf_csv, md_off, OFFLINE, recalc=False)
    md_synth = MarketDataService(Store(tmp_path / "s.db")).build(pf_synth, OFFLINE)
    u = refresh_workbook(path, pf_synth, md_synth, OFFLINE, recalc=False)
    assert u.action == "rebuilt" and "archive changed" in u.reason
    assert u.backup == tmp_path / "wb.previous.xlsx" and u.backup.exists()
    assert load_workbook(u.backup)["_state"]["B2"].value == pf_csv.signature()          # the old file is kept
    new = load_workbook(path)
    assert new["_state"]["B2"].value == pf_synth.signature() and new["Prices"]["B5"].value == "EUR"
    assert new["Portfolio"]["C1"].value == "Quantity"


def test_rebuild_of_foreign_other_version_incomplete_and_corrupt_files(pf_csv, md_off, tmp_path):
    def run(prepare):
        path = tmp_path / "wb.xlsx"
        path.unlink(missing_ok=True)
        (tmp_path / "wb.previous.xlsx").unlink(missing_ok=True)
        prepare(path)
        return refresh_workbook(path, pf_csv, md_off, OFFLINE, recalc=False), path

    u, path = run(lambda p: Workbook().save(p))                                          # somebody else's workbook
    assert u.action == "rebuilt" and "not created by this app" in u.reason and "Sheet" in load_workbook(u.backup).sheetnames
    assert open_current(path, pf_csv)[0] is not None

    def old_layout(p):
        refresh_workbook(p, pf_csv, md_off, OFFLINE, recalc=False)
        wb = load_workbook(p)
        wb["_state"]["B1"] = "ibkr-portfolio-tracker/layout-0"
        wb.save(p)
    u, _ = run(old_layout)
    assert u.action == "rebuilt" and "another layout version" in u.reason

    def no_bench(p):
        refresh_workbook(p, pf_csv, md_off, OFFLINE, recalc=False)
        wb = load_workbook(p)
        del wb["Benchmark"]
        wb.save(p)
    u, _ = run(no_bench)
    assert u.action == "rebuilt" and "lacks the sheet(s) Benchmark" in u.reason

    u, _ = run(lambda p: p.write_bytes(b"this is not a zip file"))
    assert u.action == "rebuilt" and "cannot be read" in u.reason and u.backup.read_bytes() == b"this is not a zip file"


# ======================================================================================================
# locked file / optional recalculation
# ======================================================================================================
def test_workbook_open_in_excel_raises_workbook_locked_and_changes_nothing(pf_csv, md_off, md_on, tmp_path, monkeypatch):
    path = tmp_path / "wb.xlsx"
    refresh_workbook(path, pf_csv, md_off, OFFLINE, recalc=False)
    original = path.read_bytes()
    real = os.replace

    def locked(src, dst, *a, **k):
        if Path(dst) == path:
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(WorkbookLocked, match="open in another program") as ei:
        refresh_workbook(path, pf_csv, md_on, ONLINE, recalc=False)
    assert isinstance(ei.value, PermissionError)
    assert path.read_bytes() == original and sorted(p.name for p in tmp_path.iterdir()) == ["wb.xlsx"]


def test_missing_libreoffice_is_reported_but_not_fatal(pf_csv, md_off, tmp_path, monkeypatch):
    monkeypatch.setattr("ibkr_tracker.excel.recalc.find_soffice", lambda: None)
    path = tmp_path / "wb.xlsx"
    u = refresh_workbook(path, pf_csv, md_off, OFFLINE, recalc=True)
    assert u.action == "created" and u.recalc is not None and not u.recalc.ok
    assert "LibreOffice not found" in u.messages[0] and open_current(path, pf_csv)[0] is not None


def test_inject_values_writes_cached_results_and_flags_errors(tmp_path):
    path = tmp_path / "small.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws["A1"], ws["A2"], ws["A3"], ws["A4"], ws["A5"], ws["A6"] = 1, "=A1+1", '=A2&"x"', "=1/0", "=TRUE", '=""'
    wb.save(path)
    n, errors = inject_values(path, {"S": {"A2": 2, "A3": "2x", "A4": "#DIV/0!", "A5": True}})
    assert (n, errors) == (5, 1)
    got = load_workbook(path, data_only=True)["S"]
    assert (got["A2"].value, got["A3"].value, got["A4"].value, got["A5"].value, got["A6"].value) == (2, "2x", "#DIV/0!", True, None)
    assert load_workbook(path)["S"]["A2"].value == "=A1+1"                               # the formulas are still there


@needs_lo
def test_embed_values_on_a_real_workbook_reports_no_errors(pf_csv, md_off, tmp_path):
    path = tmp_path / "wb.xlsx"
    build_workbook(pf_csv, md_off, OFFLINE).save(path)
    res = embed_values(path)
    assert res.ok and res.errors == 0 and res.formulas > 400 and "Embedded calculated values" in res.message
    assert sorted(p.name for p in tmp_path.iterdir()) == ["wb.xlsx"]


# ======================================================================================================
# overrides: workbook <-> app (three-way merge)
# ======================================================================================================
@pytest.mark.parametrize("wb_v, snap, app, expect, noted", [
    (None, None, None, None, False),                      # nothing anywhere
    (300.0, 300.0, 300.0, 300.0, False),                  # everybody agrees
    (300.0, 300.0, 310.0, 310.0, False),                  # edited in the app only -> app wins
    (310.0, 300.0, 300.0, 310.0, True),                   # edited in Excel only -> workbook wins (+ message)
    (310.0, 300.0, 320.0, 310.0, True),                   # edited in both -> the file wins
    (310.0, 300.0, 310.0, 310.0, False),                  # same edit in both -> nothing to report
    (None, 300.0, 300.0, None, True),                     # override cleared in Excel
    (None, 300.0, 310.0, None, True),                     # cleared in Excel, changed in app -> cleared
    (300.0, None, None, 300.0, True),                     # typed into a fresh cell
    (None, None, 310.0, 310.0, False),                    # app only (nothing was written to the file yet)
    (300.0 + 1e-13, 300.0, 300.0, 300.0, False),          # float noise is not an edit
])
def test_override_merge_rules(wb_v, snap, app, expect, noted):
    merged, notes = OV.merge({"px:AMZN|USD": wb_v}, {"px:AMZN|USD": snap}, {"px:AMZN|USD": app})
    got = merged["px:AMZN|USD"]
    assert (got is None and expect is None) or got == pytest.approx(expect)
    assert bool(notes) is noted
    if notes:
        assert "AMZN" in notes[0] and "workbook" in notes[0]


def test_override_merge_handles_keys_that_exist_on_one_side_only():
    merged, notes = OV.merge({"fx:HUF": 400.0}, {}, {"px:NEW|USD": 5.0, "days": 365.0})
    assert merged == {"fx:HUF": 400.0, "px:NEW|USD": 5.0, "days": 365.0} and len(notes) == 1
    assert OV.describe("fx:HUF") == "FX override USD.HUF" and OV.describe("bm") == "benchmark price override"
    assert OV._num(" 1 234,5 ") == 1234.5 and OV._num("abc") is None and OV._num(True) is None and OV._num("") is None


def test_settings_round_trip_through_the_flat_format():
    s = dict(DEFAULT_SETTINGS, price_overrides={"AMZN|USD": 300.0, "OLD|USD": 1.0}, fx_overrides={"HUF": 400.0},
             benchmark_override=None, days_per_year=360)
    flat = OV.from_settings(s, [("AMZN", "USD"), ("MSFT", "USD")], ["USD", "HUF"])
    assert flat == {"px:AMZN|USD": 300.0, "px:MSFT|USD": None, "fx:HUF": 400.0, "bm": None, "days": 360.0}
    flat["px:AMZN|USD"], flat["px:MSFT|USD"], flat["bm"] = None, 7.0, 120.0
    out = OV.into_settings(flat, s)
    assert out["price_overrides"] == {"MSFT|USD": 7.0, "OLD|USD": 1.0}                   # unknown tickers are kept
    assert out["benchmark_override"] == 120.0 and out["days_per_year"] == 360.0 and s["price_overrides"]["AMZN|USD"] == 300.0


def test_sync_overrides_ignores_missing_or_foreign_files(pf_csv, tmp_path):
    s = dict(ONLINE, price_overrides={"AMZN|USD": 1.0})
    assert sync_overrides(tmp_path / "nope.xlsx", s, pf_csv) == (s, [])
    Workbook().save(tmp_path / "foreign.xlsx")
    assert sync_overrides(tmp_path / "foreign.xlsx", s, pf_csv) == (s, [])
    (tmp_path / "junk.xlsx").write_bytes(b"junk")
    assert sync_overrides(tmp_path / "junk.xlsx", s, pf_csv) == (s, [])


def _flow(pf_csv, svc, path, settings):
    """app-side step: merge what the user typed into the file, rebuild market data, refresh the workbook"""
    merged, notes = sync_overrides(path, settings, pf_csv)
    md = svc.build(pf_csv, merged)
    return merged, notes, md, refresh_workbook(path, pf_csv, md, merged, recalc=False)


def test_values_typed_into_the_workbook_survive_refresh_and_reach_the_settings(pf_csv, sample_services, tmp_path):
    svc = sample_services[1]
    path = tmp_path / "wb.xlsx"
    _flow(pf_csv, svc, path, dict(ONLINE))
    wb = load_workbook(path)
    px = wb["Prices"]
    assert (px["B17"].value, px["K18"].value) == ("AMZN", "HUF")
    px["H17"], px["O18"], px["E14"], px["B7"] = 300, 400, 130, 360                      # as a user would in Excel
    wb.save(path)

    merged, notes, md, u = _flow(pf_csv, svc, path, dict(ONLINE))
    assert u.action == "updated" and len(notes) == 4
    assert merged["price_overrides"] == {"AMZN|USD": 300.0} and merged["fx_overrides"] == {"HUF": 400.0}
    assert merged["benchmark_override"] == 130.0 and merged["days_per_year"] == 360.0
    assert md.price("AMZN", "USD") == 300.0 and md.fx_used("HUF") == 400.0 and md.benchmark_price() == 130.0
    px2 = load_workbook(path)["Prices"]
    assert (px2["H17"].value, px2["O18"].value, px2["E14"].value, px2["B7"].value) == (300, 400, 130, 360)
    assert px2["I17"].value == '=IF(H17<>"",H17,E17)'
    assert px2["E17"].value == pytest.approx(md.raw_prices["AMZN|USD"].price)           # downloaded value kept alongside
    # a second round changes nothing and reports nothing
    merged2, notes2, md2, _ = _flow(pf_csv, svc, path, dict(merged))
    assert notes2 == [] and merged2["price_overrides"] == {"AMZN|USD": 300.0}


@needs_lo
def test_overrides_drive_the_recalculated_numbers(pf_csv, sample_services, tmp_path):
    svc = sample_services[1]
    path = tmp_path / "wb.xlsx"
    s = dict(ONLINE, price_overrides={"AMZN|USD": 300.0}, fx_overrides={"HUF": 400.0}, benchmark_override=130.0,
             days_per_year=360)
    md = svc.build(pf_csv, s)
    u = refresh_workbook(path, pf_csv, md, s, recalc=True)
    assert u.recalc.ok, u.recalc.message
    vals = sheet_values(path, "Portfolio")
    assert compare_portfolio_sheet(vals, pf_csv, compute_metrics(pf_csv, md, days_per_year=360)) == []
    assert vals["L30"] == 300.0 and vals["M42"] == pytest.approx(vals["O40"] * 400.0)   # Summary(HUF) = USD x 400
    assert vals["K46"] == pytest.approx(130.0)                                           # benchmark price used
    assert sheet_values(path, "Prices")["I17"] == 300.0


def test_app_side_override_is_written_to_the_workbook(pf_csv, sample_services, tmp_path):
    svc = sample_services[1]
    path = tmp_path / "wb.xlsx"
    _flow(pf_csv, svc, path, dict(ONLINE))
    merged, notes, md, _ = _flow(pf_csv, svc, path, dict(ONLINE, price_overrides={"MSFT|USD": 500.0}))
    assert notes == [] and merged["price_overrides"] == {"MSFT|USD": 500.0}
    px = load_workbook(path)["Prices"]
    assert (px["B21"].value, px["H21"].value) == ("MSFT", 500.0)
    assert px["H17"].value is None                                                       # others untouched


def test_clearing_an_override_in_the_workbook_removes_it_from_the_app(pf_csv, sample_services, tmp_path):
    svc = sample_services[1]
    path = tmp_path / "wb.xlsx"
    _flow(pf_csv, svc, path, dict(ONLINE, price_overrides={"AMZN|USD": 300.0}))
    wb = load_workbook(path)
    assert wb["Prices"]["H17"].value == 300.0
    wb["Prices"]["H17"] = None
    wb.save(path)
    merged, notes, md, _ = _flow(pf_csv, svc, path, dict(ONLINE, price_overrides={"AMZN|USD": 300.0}))
    assert merged["price_overrides"] == {} and "cleared" in notes[0] and md.prices["AMZN|USD"].source == "Yahoo Finance"
    assert load_workbook(path)["Prices"]["H17"].value is None
