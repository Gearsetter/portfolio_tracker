"""The application layer behind the GUI: archive, settings persistence, Refresh Prices, export toggle, overrides."""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import pytest
from openpyxl import load_workbook

from conftest import SAMPLE_CSV, SAMPLE_HTML, TODAY, needs_lo
from fakes import FakeProvider
from helpers import compare_portfolio_sheet, sheet_values
from ibkr_tracker.calc import compute_metrics
from ibkr_tracker.config import DEFAULT_SETTINGS
from ibkr_tracker.storage import Store
from ibkr_tracker.tracker import Tracker

ACCOUNT = "U24013787"
ALL_SYMBOLS = ("AMZN", "BN", "GOOGL", "META", "MSFT", "NVO", "HUF=X", "EUR=X", "VUAA.L")


def xlsx(t: Tracker, account: str = ACCOUNT) -> Path:
    return t.workbook_path(t.portfolio(account))


# ======================================================================================================
# archive
# ======================================================================================================
def test_import_is_idempotent_and_keeps_both_formats(tracker):
    r1 = tracker.import_statement(SAMPLE_CSV.name, SAMPLE_CSV.read_bytes())
    assert r1.ok and r1.is_new and r1.account == ACCOUNT and "22 stock trades" in r1.message
    again = tracker.import_statement(SAMPLE_CSV.name, SAMPLE_CSV.read_bytes())
    assert again.ok and not again.is_new and "already in the archive" in again.message
    r2 = tracker.import_statement(SAMPLE_HTML.name, SAMPLE_HTML.read_bytes())
    assert r2.ok and r2.is_new
    infos = tracker.statements(ACCOUNT)
    assert [(s.fmt, s.account, s.n_trades) for s in infos] == [("csv", ACCOUNT, 22), ("html", ACCOUNT, 22)]
    p = tracker.portfolio(ACCOUNT)
    assert len(p.trades) == 22 and len(p.dividends) == 10 and p.duplicates_dropped > 0     # merged, not doubled


def test_bad_files_give_friendly_messages(tracker):
    junk = tracker.import_statement("notes.txt", b"hello world")
    assert not junk.ok and junk.message.startswith("notes.txt:") and tracker.statements() == []
    empty = tracker.import_statement("empty.csv", b"")
    assert not empty.ok
    res = tracker.import_files([("a.txt", b"x"), (SAMPLE_CSV.name, SAMPLE_CSV.read_bytes())])
    assert [r.ok for r in res] == [False, True]


def test_archive_survives_a_restart_and_rebuilds_the_same_portfolio(loaded, home, fake):
    before = loaded.portfolio(ACCOUNT)
    again = Tracker(Store(home / "tracker.db"), fake, today=lambda: TODAY)                 # "restart"
    after = again.portfolio(ACCOUNT)
    assert after.signature() == before.signature() and len(after.trades) == 22
    assert [(t.ticker, t.quantity, t.price, t.fee, t.dt) for t in after.trades] == \
           [(t.ticker, t.quantity, t.price, t.fee, t.dt) for t in before.trades]
    assert after.cash_balances() == before.cash_balances()
    assert again.accounts()[0][:2] == (ACCOUNT, "TBSZ-2026") and again.default_account() == ACCOUNT


def test_stored_statements_give_the_same_numbers_as_freshly_parsed_ones(loaded, pf_both):
    assert loaded.portfolio(ACCOUNT).signature() == pf_both.signature()
    snap = loaded.snapshot(ACCOUNT, dict(loaded.settings(), price_source="statement"))
    assert snap.problem == "" and snap.metrics.usd_total == pytest.approx(9641.1039, abs=1e-3)


def test_delete_statement_and_default_account(loaded, tracker):
    ids = [s.id for s in tracker.statements(ACCOUNT)]
    tracker.update_settings(last_account="NOPE")
    assert tracker.default_account() == ACCOUNT                                          # unknown last account is ignored
    tracker.update_settings(last_account=ACCOUNT)
    tracker.delete_statement(ids[0])
    assert len(tracker.statements(ACCOUNT)) == 1 and tracker.portfolio(ACCOUNT) is not None
    tracker.delete_statement(ids[1])
    assert tracker.portfolio(ACCOUNT) is None and tracker.default_account() is None and tracker.accounts() == []
    out = tracker.refresh_prices(ACCOUNT)
    assert not out.ok and out.errors == ["No statements imported yet."] and out.snapshot is None
    assert tracker.export(ACCOUNT).errors == ["No statements imported yet."] and tracker.snapshot(ACCOUNT) is None


# ======================================================================================================
# settings are remembered between sessions
# ======================================================================================================
def test_settings_defaults_persist_and_ignore_unknown_keys(home, fake):
    t = Tracker(Store(home / "tracker.db"), fake)
    assert t.settings() == DEFAULT_SETTINGS and DEFAULT_SETTINGS["export_excel"] is True
    t.update_settings(export_excel=False, benchmark_symbol="VUAA.DE", days_per_year=360,
                      price_overrides={"AMZN|USD": 300.0}, bogus="x")
    again = Tracker(Store(home / "tracker.db"), fake).settings()                         # new session, same database
    assert again["export_excel"] is False and again["benchmark_symbol"] == "VUAA.DE" and again["days_per_year"] == 360
    assert again["price_overrides"] == {"AMZN|USD": 300.0} and "bogus" not in again
    assert again["price_source"] == "yahoo" and again["recalc_with_libreoffice"] is True      # untouched defaults stay


def test_market_cache_survives_a_restart(loaded, home, fake):
    out = loaded.refresh_prices(ACCOUNT)
    assert out.ok and out.snapshot.md.mode.startswith("Yahoo")
    offline_provider = FakeProvider(fail=ALL_SYMBOLS)                                    # the new session has no network
    again = Tracker(Store(home / "tracker.db"), offline_provider, today=lambda: TODAY)
    snap = again.snapshot(ACCOUNT)
    assert snap.md.as_of == TODAY and snap.md.prices["AMZN|USD"].source == "Yahoo Finance"   # served from the cache
    assert offline_provider.calls == [] and snap.metrics.usd_total == pytest.approx(out.snapshot.metrics.usd_total)


# ======================================================================================================
# Refresh Prices
# ======================================================================================================
def test_refresh_prices_end_to_end(loaded, fake):
    out = loaded.refresh_prices(ACCOUNT)
    assert out.ok and out.errors == [] and out.report.summary() == "Updated 9 symbol(s)."
    assert set(out.report.fetched) == set(ALL_SYMBOLS)
    assert out.update.action == "created" and out.update.path == xlsx(loaded) and xlsx(loaded).exists()
    snap = out.snapshot
    assert snap.problem == "" and snap.md.as_of == TODAY and snap.metrics.base_currency == "HUF"
    assert all(r.status in ("OK", "INFO") for r in snap.checks)
    hist = loaded.history(ACCOUNT)
    assert len(hist) == 1 and hist[0]["as_of"] == TODAY.isoformat()
    assert hist[0]["usd_total"] == pytest.approx(snap.metrics.usd_total) and hist[0]["base_currency"] == "HUF"
    wb = load_workbook(xlsx(loaded))
    assert wb["Prices"]["B8"].value.startswith("Yahoo Finance") and wb["Prices"]["G17"].value == "Yahoo Finance"


def test_second_refresh_downloads_only_the_tail_and_updates_the_workbook_in_place(loaded, fake):
    loaded.refresh_prices(ACCOUNT)
    wb1 = load_workbook(xlsx(loaded))
    portfolio_cells = {c.coordinate: c.value for row in wb1["Portfolio"].iter_rows() for c in row}
    fake.calls.clear()
    out = loaded.refresh_prices(ACCOUNT)
    assert out.ok and out.update.action == "updated" and out.update.backup is None
    assert len(fake.calls) == 9 and {c[1] for c in fake.calls} == {TODAY - timedelta(days=7)}
    wb2 = load_workbook(xlsx(loaded))
    assert {c.coordinate: c.value for row in wb2["Portfolio"].iter_rows() for c in row} == portfolio_cells
    assert len(loaded.history(ACCOUNT)) == 1                                              # same valuation day: replaced
    assert [p.name for p in xlsx(loaded).parent.iterdir()] == [xlsx(loaded).name]         # no temp / backup files


def test_new_trading_day_adds_a_history_point(loaded, fake):
    loaded.refresh_prices(ACCOUNT)
    next_day = date(2026, 11, 2)
    fake.last_day = next_day
    nxt = Tracker(loaded.store, fake, today=lambda: next_day)
    out = nxt.refresh_prices(ACCOUNT)
    assert out.snapshot.md.as_of == next_day
    assert [h["as_of"] for h in nxt.history(ACCOUNT)] == [TODAY.isoformat(), next_day.isoformat()]


def test_export_toggle_off_calculates_but_writes_no_file(loaded, fake):
    loaded.update_settings(export_excel=False)
    out = loaded.refresh_prices(ACCOUNT)
    assert out.ok and out.update is None and not xlsx(loaded).exists()
    assert out.snapshot.metrics is not None and len(loaded.history(ACCOUNT)) == 1       # the numbers are still tracked
    assert loaded.workbook_status(loaded.portfolio(ACCOUNT))[0] == "missing"
    calls = len(fake.calls)
    loaded.update_settings(export_excel=True)
    ex = loaded.export(ACCOUNT)                                                           # explicit export: no download
    assert ex.ok and ex.update.action == "created" and xlsx(loaded).exists() and len(fake.calls) == calls


def test_workbook_status_follows_the_archive(loaded):
    p = loaded.portfolio(ACCOUNT)
    assert loaded.workbook_status(p)[0] == "missing"
    loaded.export(ACCOUNT)
    state, detail, path = loaded.workbook_status(loaded.portfolio(ACCOUNT))
    assert (state, path) == ("current", xlsx(loaded)) and "matches the current statement archive" in detail
    loaded.delete_statement(loaded.statements(ACCOUNT)[0].id)                  # CSV gone, HTML only: other numbers
    state, detail, _ = loaded.workbook_status(loaded.portfolio(ACCOUNT))
    assert state == "stale" and "archive changed" in detail
    out = loaded.export(ACCOUNT)
    assert out.update.action == "rebuilt" and out.update.backup.exists()
    assert loaded.workbook_status(loaded.portfolio(ACCOUNT))[0] == "current"


def test_statements_only_mode_never_downloads(loaded, fake):
    loaded.update_settings(price_source="statement")
    out = loaded.refresh_prices(ACCOUNT)
    assert out.ok and out.report.offline and fake.calls == []
    assert any("statements only" in m for m in out.messages)
    assert out.snapshot.md.mode.startswith("Statements only") and out.snapshot.md.as_of == date(2026, 10, 23)
    assert out.update.action == "created"                                                 # the workbook is still written
    assert load_workbook(xlsx(loaded))["Prices"]["G17"].value.startswith("IBKR statement")


def test_yahoo_outage_degrades_to_statement_prices(loaded):
    broken = FakeProvider(fail=ALL_SYMBOLS)
    t = Tracker(loaded.store, broken, today=lambda: TODAY)
    out = t.refresh_prices(ACCOUNT)
    assert out.ok and out.report.aborted and len(broken.calls) == 3                       # gave up after 3 failures
    assert any("could not be reached" in m for m in out.messages)
    assert out.snapshot.md.as_of == date(2026, 10, 23) and out.snapshot.metrics is not None
    assert out.update.action == "created" and load_workbook(xlsx(t))["Prices"]["G17"].value.startswith("IBKR statement")


def test_partial_outage_is_reported_but_everything_else_updates(loaded):
    t = Tracker(loaded.store, FakeProvider(fail=("META",), last_day=TODAY), today=lambda: TODAY)
    out = t.refresh_prices(ACCOUNT)
    assert out.ok and set(out.report.errors) == {"META"} and len(out.report.fetched) == 8
    assert any("could not be downloaded" in m for m in out.messages)
    md = out.snapshot.md
    assert md.prices["META|USD"].source.startswith("IBKR statement") and md.prices["MSFT|USD"].source == "Yahoo Finance"


def test_other_benchmark_symbol_is_used(loaded, fake):
    loaded.update_settings(benchmark_symbol="VUAA.DE")
    out = loaded.refresh_prices(ACCOUNT)
    assert out.snapshot.md.benchmark.symbol == "VUAA.DE" and out.snapshot.md.benchmark.currency == "EUR"
    assert "VUAA.DE" in {c[0] for c in fake.calls}
    assert load_workbook(xlsx(loaded))["Prices"]["A14"].value == "VUAA.DE"


def test_locked_workbook_is_an_error_but_the_numbers_are_still_returned(loaded, monkeypatch):
    loaded.export(ACCOUNT)
    original = xlsx(loaded).read_bytes()
    real = os.replace

    def locked(src, dst, *a, **k):
        if Path(dst) == xlsx(loaded):
            raise PermissionError(13, "in use")
        return real(src, dst, *a, **k)

    with monkeypatch.context() as m:                                                      # (undo() would also drop `home`)
        m.setattr(os, "replace", locked)
        out = loaded.refresh_prices(ACCOUNT)
    assert not out.ok and "open in another program" in out.errors[0]
    assert out.snapshot.metrics is not None and out.update is None                       # dashboard keeps working
    assert xlsx(loaded).read_bytes() == original
    assert loaded.refresh_prices(ACCOUNT).ok                                              # works again after closing Excel


def test_overrides_typed_into_the_workbook_are_adopted_and_remembered(loaded):
    loaded.refresh_prices(ACCOUNT)
    wb = load_workbook(xlsx(loaded))
    wb["Prices"]["H17"] = 300
    wb.save(xlsx(loaded))
    out = loaded.refresh_prices(ACCOUNT)
    assert out.ok and any("price override AMZN (USD) from the workbook: 300" in m for m in out.messages)
    assert loaded.settings()["price_overrides"] == {"AMZN|USD": 300.0}                   # persisted for the next session
    assert out.snapshot.md.price("AMZN", "USD") == 300.0
    assert load_workbook(xlsx(loaded))["Prices"]["H17"].value == 300
    # overrides set in the app go the other way
    loaded.update_settings(fx_overrides={"HUF": 400.0})
    out2 = loaded.refresh_prices(ACCOUNT)
    assert out2.snapshot.md.fx_used("HUF") == 400.0 and load_workbook(xlsx(loaded))["Prices"]["O18"].value == 400.0


def test_missing_fx_is_reported_not_raised(loaded, monkeypatch):
    from ibkr_tracker.market.data import MarketData
    snap = loaded.snapshot(ACCOUNT, dict(loaded.settings(), price_source="statement"))
    assert snap.problem == ""
    real_build = loaded.service.build

    def build_without_fx(p, s):
        md = real_build(p, s)
        md.missing_fx = ["CHF"]
        return md

    monkeypatch.setattr(loaded.service, "build", build_without_fx)
    bad = loaded.snapshot(ACCOUNT)
    assert bad.metrics is None and "No FX rate for CHF" in bad.problem and bad.checks == []
    out = loaded.export(ACCOUNT)
    assert not out.ok and "CHF" in out.errors[0] and not xlsx(loaded).exists()


@needs_lo
def test_export_with_libreoffice_embeds_calculated_values(loaded):
    loaded.update_settings(recalc_with_libreoffice=True)
    out = loaded.refresh_prices(ACCOUNT)
    assert out.ok and out.update.recalc.ok and out.update.recalc.errors == 0, out.update.recalc.message
    vals = sheet_values(xlsx(loaded), "Portfolio")
    assert compare_portfolio_sheet(vals, out.snapshot.portfolio, out.snapshot.metrics) == []
    assert loaded.libreoffice_available() is True
    assert sheet_values(xlsx(loaded), "Checks")["B4"] == "all checks passed"
