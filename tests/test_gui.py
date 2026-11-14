"""Streamlit GUI: headless script tests (``AppTest``) and an optional real-browser smoke test (Playwright).

Yahoo Finance is replaced by the deterministic fake - the tests never touch the network."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from conftest import ROOT, SAMPLE_CSV, SAMPLE_HTML
from fakes import FakeProvider
from ibkr_tracker.market.providers import YahooProvider
from ibkr_tracker.storage import Store
from ibkr_tracker.tracker import Tracker

APP = str(ROOT / "app.py")
ACCOUNT = "U24013787"
LAST_DAY = date(2026, 10, 30)


@pytest.fixture
def gui(home, monkeypatch):
    """Data folder with the two sample statements imported, LibreOffice embedding off, fake Yahoo."""
    st.cache_resource.clear()
    fake = FakeProvider(last_day=LAST_DAY)
    monkeypatch.setattr(YahooProvider, "history",
                        lambda self, symbol, start, end, include_intraday=False: fake.history(symbol, start, end,
                                                                                               include_intraday))
    t = Tracker(Store(home / "tracker.db"))
    t.update_settings(recalc_with_libreoffice=False)
    for f in (SAMPLE_CSV, SAMPLE_HTML):
        assert t.import_statement(f.name, f.read_bytes()).ok
    yield t, fake, home
    st.cache_resource.clear()


def new_app() -> AppTest:
    return AppTest.from_file(APP, default_timeout=90)


def kpis(at: AppTest) -> dict[str, str]:
    return {m.label: m.value for m in at.metric}


def sidebar_widget(at: AppTest, kind: str, key: str):
    return next(w for w in getattr(at.sidebar, kind) if w.key == key)


def texts(elements) -> list[str]:
    return [e.value for e in elements]


# ======================================================================================================
# AppTest
# ======================================================================================================
def test_welcome_screen_without_statements(home, monkeypatch):
    st.cache_resource.clear()
    at = new_app().run()
    assert not at.exception
    assert any("Welcome" in m.value for m in at.markdown) and sidebar_widget(at, "button", "refresh").disabled
    assert texts(at.sidebar.info) == ["No statements yet - upload one or more Activity Statements above."]
    st.cache_resource.clear()


def test_dashboard_for_the_imported_account(gui):
    at = new_app().run()
    assert not at.exception, [e.value for e in at.exception]
    labels = [t.label for t in at.tabs]                                               # nested tabs are listed too
    assert [x for x in labels if not x[0].isdigit()] == ["Dashboard", "Transactions", "Reconciliation", "Market data",
                                                         "Archive", "Excel export"]
    assert [x for x in labels if x[0].isdigit()] == ["1 · Stock transactions (+ 5 · extensions)", "2 · Dividends",
                                                     "3 · Deposits & withdrawals", "4 · Forex"]
    k = kpis(at)
    assert k["Total value (USD)"] == "9,641.10 USD" and k["Net deposits (HUF)"] == "2,800,000 HUF"
    assert k["Return (HUF)"].endswith("x") and k["CAGR (USD)"].endswith("%") and k["Stocks (USD)"] == "9,608.62 USD"
    assert k["VUAA instead (USD)"] == "n/a"                                           # nothing downloaded yet
    assert sidebar_widget(at, "selectbox", "account").value == ACCOUNT
    assert sidebar_widget(at, "toggle", "w_export").value is True                    # export toggle defaults to on
    assert "base currency HUF" in " ".join(texts(at.caption))
    assert any("all checks passed" in s.value for s in at.success)                  # Reconciliation tab
    assert any(f"{ACCOUNT}" in c for c in texts(at.caption))


def test_refresh_prices_downloads_exports_and_updates_in_place(gui):
    t, fake, home = gui
    at = new_app().run()
    sidebar_widget(at, "button", "refresh").click().run()
    assert not at.exception, [e.value for e in at.exception]
    out = home / "output" / f"IBKR_Portfolio_{ACCOUNT}.xlsx"
    assert out.exists() and len(fake.calls) == 9
    assert any("Updated 9 symbol(s)." in s.value for s in at.success)
    assert any("Refresh: Excel workbook created" in s.value for s in at.success)
    k = kpis(at)
    assert k["Total value (USD)"] != "9,641.10 USD" and k["VUAA instead (USD)"] != "n/a"
    assert k["Stocks minus VUAA (USD)"] != "n/a"
    assert "Valuation date 2026-10-30" in " ".join(texts(at.caption))
    first_mtime = out.stat().st_mtime_ns

    fake.calls.clear()
    sidebar_widget(at, "button", "refresh").click().run()
    assert not at.exception
    assert any("updated in place" in s.value for s in at.success), texts(at.success)
    assert len(fake.calls) == 9 and out.stat().st_mtime_ns > first_mtime


def test_export_toggle_controls_the_file_and_is_remembered(gui):
    t, fake, home = gui
    out = home / "output" / f"IBKR_Portfolio_{ACCOUNT}.xlsx"
    at = new_app().run()
    sidebar_widget(at, "toggle", "w_export").set_value(False).run()
    assert not at.exception and t.settings()["export_excel"] is False                # persisted at once
    sidebar_widget(at, "button", "refresh").click().run()
    assert not at.exception and not out.exists() and not any("Excel workbook" in s.value for s in at.success)
    assert kpis(at)["VUAA instead (USD)"] != "n/a"                                    # numbers are updated regardless
    at2 = new_app().run()                                                             # a new browser session
    assert sidebar_widget(at2, "toggle", "w_export").value is False
    sidebar_widget(at2, "toggle", "w_export").set_value(True).run()                  # switching it on writes the file now
    assert out.exists() and any("Export: Excel workbook created" in s.value for s in at2.success)


def test_settings_survive_a_new_session(gui):
    t, fake, home = gui
    at = new_app().run()
    sidebar_widget(at, "text_input", "w_bench").set_value("SPY").run()
    sidebar_widget(at, "number_input", "w_days").set_value(365.25).run()
    assert not at.exception and t.settings()["benchmark_symbol"] == "SPY" and t.settings()["days_per_year"] == 365.25
    st.cache_resource.clear()                                                         # "restart the app"
    again = new_app().run()
    assert sidebar_widget(again, "text_input", "w_bench").value == "SPY"
    assert sidebar_widget(again, "number_input", "w_days").value == 365.25
    assert sidebar_widget(again, "selectbox", "account").value == ACCOUNT


def test_statements_only_mode_makes_no_requests(gui):
    t, fake, home = gui
    at = new_app().run()
    sidebar_widget(at, "selectbox", "w_source").set_value("statement").run()
    sidebar_widget(at, "button", "refresh").click().run()
    assert not at.exception and fake.calls == []
    assert any("statements only" in i.value for i in at.info) and t.settings()["price_source"] == "statement"
    assert "Statements only (offline mode)" in " ".join(texts(at.caption))


def test_yahoo_outage_is_handled_gracefully(gui, monkeypatch):
    t, fake, home = gui
    monkeypatch.setattr(YahooProvider, "history", lambda self, *a, **k: (_ for _ in ()).throw(
        RuntimeError("Failed to connect to query1.finance.yahoo.com")))
    at = new_app().run()
    sidebar_widget(at, "button", "refresh").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("could not be reached" in i.value for i in at.info), texts(at.info)
    assert (home / "output" / f"IBKR_Portfolio_{ACCOUNT}.xlsx").exists()               # the workbook is still produced
    assert kpis(at)["Total value (USD)"] == "9,641.10 USD"                             # statement prices


def test_archive_tab_removes_a_statement(gui):
    t, fake, home = gui
    at = new_app().run()
    ids = [s.id for s in t.statements(ACCOUNT)]
    assert len(ids) == 2
    next(b for b in at.button if b.key == f"del_{ids[1]}").click().run()
    assert not at.exception and len(t.statements(ACCOUNT)) == 1
    assert any("Removed" in i.value for i in at.info)
    assert any("Archive change: Excel workbook created" in s.value for s in at.success)


def test_export_tab_buttons(gui):
    t, fake, home = gui
    t.update_settings(export_excel=False)
    at = new_app().run()
    assert any("No workbook has been generated yet" in i.value for i in at.info)
    next(b for b in at.button if b.key == "export_now").click().run()
    assert not at.exception and (home / "output" / f"IBKR_Portfolio_{ACCOUNT}.xlsx").exists()
    assert any("The workbook is up to date" in s.value for s in at.success)


# ======================================================================================================
# real browser (optional)
# ======================================================================================================
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(home):
    """Streamlit started exactly like a user would (``streamlit run app.py``) on a private data folder."""
    t = Tracker(Store(home / "tracker.db"))
    t.update_settings(price_source="statement", recalc_with_libreoffice=False)            # deterministic, no network
    for f in (SAMPLE_CSV, SAMPLE_HTML):
        t.import_statement(f.name, f.read_bytes())
    port = _free_port()
    env = dict(os.environ, IBKR_TRACKER_HOME=str(home))
    proc = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", APP, "--server.port", str(port), "--server.headless", "true",
         "--browser.gatherUsageStats", "false", "--server.fileWatcherType", "none"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(ROOT))
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if urllib.request.urlopen(url + "/_stcore/health", timeout=1).read() == b"ok":
                    break
            except Exception:                                                              # noqa: BLE001
                time.sleep(0.3)
        else:
            pytest.fail("Streamlit did not start")
        yield url, home
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.mark.gui
def test_browser_smoke(server):
    sync_api = pytest.importorskip("playwright.sync_api")
    url, home = server
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:                                                           # noqa: BLE001
            pytest.skip(f"Chromium is not available: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1500, "height": 1000})
            page.goto(url, wait_until="networkidle")
            for text in ("Total value (USD)", "9,641.10 USD", "Export results as Excel file"):          # (rendered in steps)
                page.get_by_text(text).first.wait_for(timeout=30000)
            assert page.get_by_role("button", name=re.compile("Refresh Prices")).count() == 1
            assert page.locator("[data-testid='stException']").count() == 0
            for tab in ("Transactions", "Reconciliation", "Market data", "Archive", "Excel export"):
                page.get_by_role("tab", name=tab).click()
                page.wait_for_timeout(400)
                assert page.locator("[data-testid='stException']").count() == 0, tab
            page.get_by_role("tab", name="Dashboard").click()
            page.get_by_role("button", name=re.compile("Refresh Prices")).click()
            page.get_by_text("nothing was downloaded").first.wait_for(timeout=30000)         # message of the refresh
            page.get_by_text("Excel workbook created").first.wait_for(timeout=30000)
            assert (home / "output" / f"IBKR_Portfolio_{ACCOUNT}.xlsx").exists()
            assert page.locator("[data-testid='stException']").count() == 0
        finally:
            browser.close()
