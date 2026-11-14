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
    assert labels[0] == "Dashboard"
    top = [x for x in labels if x in {"Dashboard", "Transactions", "Reconciliation", "Market data", "Archive",
                                      "Excel export"}]
    assert top == ["Dashboard", "Transactions", "Reconciliation", "Market data", "Archive", "Excel export"]
    assert [x for x in labels if x[0].isdigit()] == ["1 · Stock transactions (+ 5 · extensions)", "2 · Dividends",
                                                     "3 · Deposits & withdrawals", "4 · Forex"]
    assert labels[labels.index("Dashboard") + 1:labels.index("Transactions")] == [
        "Value & deposits", "Gain", "Return", "CAGR", "vs. VUAA", "Allocation"]               # the history charts
    assert labels[labels.index("Market data") + 1:labels.index("Archive")] == [
        "Prices & overrides", "Ticker mapping", "Manual price history", "Paste text", "Upload a file", "Type rows",
        "Price coverage"]
    k = kpis(at)
    assert k["Total value (USD)"] == "9,641.10 USD" and k["Net deposits (HUF)"] == "2,800,000 HUF"
    assert k["Return (HUF)"].endswith("x") and k["CAGR (USD)"].endswith("%") and k["Stocks (USD)"] == "9,608.62 USD"
    assert k["VUAA instead (USD)"] == "n/a" and k["VUAA CAGR (USD)"] == "n/a"        # nothing downloaded yet
    assert "Stocks minus VUAA (USD)" not in k                                         # no longer a primary KPI ...
    secondary = [m.value for m in at.markdown if "sec-label" in m.value]
    assert any("Stocks minus VUAA (USD)" in v for v in secondary)                    # ... but in the secondary card
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
    assert k["VUAA CAGR (USD)"] != "n/a" and k["VUAA CAGR (USD)"].endswith("%")      # the primary benchmark figure
    assert "Stocks minus VUAA (USD)" not in k
    secondary = " ".join(m.value for m in at.markdown if "sec-label" in m.value)
    assert "Stocks minus VUAA (USD)" in secondary and "n/a</div>" not in secondary.split("Stocks minus")[1][:200]
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


# ---- performance history -------------------------------------------------------------------------------------
def charts(at: AppTest):
    return at.get("vega_lite_chart")


def captions(at: AppTest) -> str:
    return " ".join(texts(at.caption))


def test_history_charts_cover_the_lifespan_before_any_refresh(gui):
    t, fake, home = gui
    at = new_app().run()
    assert not at.exception, [e.value for e in at.exception]
    days = (date(2026, 10, 23) - date(2026, 1, 14)).days + 1                       # first transaction -> statement date
    assert (f"Rebuilt day by day from the first transaction (2026-01-14) to the valuation date (2026-10-23) - "
            f"{days:,} days") in captions(at)
    assert len(charts(at)) >= 5 and not any("differs from the headline" in w.value for w in at.warning)
    assert any(n.value.startswith("No daily prices have been downloaded yet") for n in at.warning)   # one clear note
    assert sum("Days without any price" in w.value for w in at.warning) == 0
    assert "⬇  Download the daily history (CSV)" in [b.proto.label for b in at.get("download_button")]


def test_history_follows_a_refresh_and_gains_the_benchmark_chart(gui):
    t, fake, home = gui
    at = new_app().run()
    before = len(charts(at))
    sidebar_widget(at, "button", "refresh").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert "to the valuation date (2026-10-30)" in captions(at)
    assert len(charts(at)) > before                                                  # the "vs. VUAA" tab has charts now
    assert not any(w.value.startswith("No daily prices have been downloaded yet") for w in at.warning)
    assert not any("differs from the headline" in w.value for w in at.warning)
    h = t.snapshot(ACCOUNT).history()
    assert h.start == date(2026, 1, 14) and h.end == date(2026, 10, 30) and h.matches(t.snapshot(ACCOUNT).metrics) == []


def test_the_chart_controls_rerun_cleanly(gui):
    t, fake, home = gui
    at = new_app().run()
    sidebar_widget(at, "button", "refresh").click().run()
    unit = next(r for r in at.radio if r.key == "hist_unit")
    period = next(r for r in at.radio if r.key == "hist_period")
    assert unit.options == ["HUF", "USD"] and period.options == ["All", "1Y", "YTD", "6M", "3M", "1M"]
    for u in ("USD", "HUF"):
        next(r for r in at.radio if r.key == "hist_unit").set_value(u).run()
        for per in ("1M", "YTD", "3M", "All"):
            next(r for r in at.radio if r.key == "hist_period").set_value(per).run()
            assert not at.exception, (u, per, [e.value for e in at.exception])
    assert kpis(at)["Total value (USD)"] != "9,641.10 USD"                             # the headline is not affected


def test_the_history_does_not_depend_on_the_view_filter(gui):
    t, fake, home = gui
    at = new_app().run()
    sidebar_widget(at, "button", "refresh").click().run()
    on = t.snapshot(ACCOUNT, dict(t.settings(), hide_closed_dust=True)).history().frame
    sidebar_widget(at, "toggle", "w_hide").set_value(False).run()
    assert not at.exception
    off = t.snapshot(ACCOUNT, dict(t.settings(), hide_closed_dust=False)).history().frame
    assert on.equals(off)


# ---- manual price history -------------------------------------------------------------------------------------
def widget(at: AppTest, kind: str, key: str):
    return next(w for w in getattr(at, kind) if w.key == key)


def paste(at: AppTest, stock: str, text: str, nonce: int = 0) -> AppTest:
    widget(at, "selectbox", "mp_target").set_value(stock).run()
    return widget(at, "text_area", f"mp_text_{nonce}").set_value(text).run()


def test_pasted_prices_are_saved_and_used_at_once(gui):
    t, fake, home = gui
    at = new_app().run()
    stocks_before = kpis(at)["Stocks (USD)"]
    at = paste(at, "MSFT (USD)", "Date;Price\n2026-10-28;500,25\n2026-10-29;501,75\n")
    assert not at.exception, [e.value for e in at.exception]
    assert any(s.value.startswith("2 price(s) from 2026-10-28 to 2026-10-29") for s in at.success)
    assert t.manual_prices("MSFT|USD") == []                                         # nothing is saved before the button
    widget(at, "button", "mp_save").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert t.manual_prices("MSFT|USD") == [(date(2026, 10, 28), 500.25), (date(2026, 10, 29), 501.75)]
    assert any("2 manual price(s) saved for MSFT (USD)" in s.value for s in at.success)
    snap = t.snapshot(ACCOUNT)
    assert snap.md.price("MSFT", "USD") == 501.75 and snap.md.as_of == date(2026, 10, 29)
    assert kpis(at)["Stocks (USD)"] == f"{snap.metrics.stocks_usd:,.2f} USD" != stocks_before
    assert "Valuation date 2026-10-29" in captions(at)
    assert not any("differs from the headline" in w.value for w in at.warning)
    assert (home / "output" / f"IBKR_Portfolio_{ACCOUNT}.xlsx").exists()                # the workbook follows
    again = new_app().run()                                                          # a new session: still valued so
    assert kpis(again)["Stocks (USD)"] == kpis(at)["Stocks (USD)"]
    widget(again, "selectbox", "mp_target").set_value("MSFT (USD)").run()
    assert "Saved manual prices of MSFT (USD) (2)" in [e.label for e in again.expander]


def test_a_paste_with_unusable_lines_needs_a_confirmation(gui):
    t, fake, home = gui
    at = paste(new_app().run(), "MSFT (USD)", "2026-10-28,500.25\nnonsense\n2026-10-29,501.75\n")
    assert not at.exception
    assert widget(at, "button", "mp_save").disabled                                   # not until the user agrees
    assert any("Skip the 1 unusable line(s) and save the rest" in c.label for c in at.checkbox)
    widget(at, "checkbox", "mp_skip_0").check().run()
    assert not widget(at, "button", "mp_save").disabled
    widget(at, "button", "mp_save").click().run()
    assert [d for d, _ in t.manual_prices("MSFT|USD")] == [date(2026, 10, 28), date(2026, 10, 29)]


def test_a_paste_that_cannot_be_read_saves_nothing(gui):
    t, fake, home = gui
    at = paste(new_app().run(), "MSFT (USD)", "nothing\nuseful\nhere\n")
    assert not at.exception and any("Expected two columns" in e.value for e in at.error)
    assert not any(b.key == "mp_save" for b in at.button) and t.manual_prices("MSFT|USD") == []
    at = paste(at, "MSFT (USD)", "Date,Price\n")                                       # a header and nothing else
    assert any("Only a header line" in e.value for e in at.error) and t.manual_prices("MSFT|USD") == []
    at = paste(at, "MSFT (USD)", "2026-13-45,10\n2026-14-45,11\n")                   # the right shape, no valid day
    assert any("No valid price rows were found." in e.value for e in at.error)
    assert not any(b.key == "mp_save" for b in at.button) and t.manual_prices("MSFT|USD") == []


def test_prices_can_be_added_replaced_and_deleted(gui):
    t, fake, home = gui
    t.save_manual_prices("MSFT|USD", [(date(2026, 10, 1), 1.0), (date(2026, 10, 2), 2.0)])
    at = paste(new_app().run(), "MSFT (USD)", "2026-10-02,22\n2026-10-03,33\n")
    widget(at, "button", "mp_save").click().run()                                    # default mode: add / overwrite days
    assert t.manual_prices("MSFT|USD") == [(date(2026, 10, 1), 1.0), (date(2026, 10, 2), 22.0), (date(2026, 10, 3), 33.0)]
    at = paste(at, "MSFT (USD)", "2026-10-05,55\n", nonce=1)
    widget(at, "radio", "mp_mode_1").set_value("Replace all manual prices of this stock").run()
    widget(at, "button", "mp_save").click().run()
    assert t.manual_prices("MSFT|USD") == [(date(2026, 10, 5), 55.0)]
    at = widget(at, "selectbox", "mp_target").set_value("MSFT (USD)").run()
    assert widget(at, "button", "mp_delete").disabled
    widget(at, "checkbox", "mp_sure_2").check().run()
    widget(at, "button", "mp_delete").click().run()
    assert not at.exception and t.manual_prices("MSFT|USD") == []
    assert any("1 manual price(s) of MSFT (USD) deleted." in i.value for i in at.info)


def test_the_benchmark_gets_its_own_manual_history(gui):
    t, fake, home = gui
    target = next(o for o in widget(new_app().run(), "selectbox", "mp_target").options if o.startswith("Benchmark"))
    at = paste(new_app().run(), target, "2026-01-14,500\n2026-10-23,600\n")
    widget(at, "button", "mp_save").click().run()
    assert not at.exception, [e.value for e in at.exception]
    from ibkr_tracker.market.manual import BENCHMARK_KEY
    assert t.manual_prices(BENCHMARK_KEY) == [(date(2026, 1, 14), 500.0), (date(2026, 10, 23), 600.0)]
    snap = t.snapshot(ACCOUNT)
    assert snap.metrics.benchmark.available and snap.history().benchmark_available


# ---- ticker mapping -------------------------------------------------------------------------------------------
def edit_mapping(at: AppTest, t: Tracker, ticker: str, alias=None, manual=None, nonce: int = 0) -> AppTest:
    rows = t.mapping_rows(t.portfolio(ACCOUNT))
    idx = next(i for i, r in enumerate(rows) if r.ticker == ticker)
    change = {}
    if alias is not None:
        change["Your symbol (alias)"] = alias
    if manual is not None:
        change["Manual prices only"] = manual
    at.session_state[f"map_editor_{nonce}"] = {"edited_rows": {idx: change}, "added_rows": [], "deleted_rows": []}
    return at.run()


def test_the_mapping_tab_opens_with_every_stock_on_its_automatic_symbol(gui):
    t, fake, home = gui
    at = new_app().run()
    assert not at.exception
    rows = t.mapping_rows(t.portfolio(ACCOUNT))
    assert [r.ticker for r in rows] == ["AMZN", "BN", "GOOGL", "META", "MSFT", "NVO"]
    assert all(r.symbol == r.ticker and r.source == "automatic" for r in rows)
    assert widget(at, "checkbox", "map_download").value is True and not widget(at, "button", "map_test").disabled
    assert "⬇  Download ticker_mapping.csv" in [b.proto.label for b in at.get("download_button")]


def test_saving_an_alias_downloads_only_that_symbol_and_persists(gui):
    t, fake, home = gui
    at = edit_mapping(new_app().run(), t, "NVO", alias="nvo.mx")
    assert not at.exception, [e.value for e in at.exception]
    widget(at, "button", "map_save").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert t.settings()["yahoo_symbols"] == {"NVO|USD": "NVO.MX"} and [c[0] for c in fake.calls] == ["NVO.MX"]
    assert any("Mapping saved (1 stock(s) changed)." in s.value for s in at.success)
    st.cache_resource.clear()                                                         # "restart the app"
    again = new_app().run()
    assert next(r for r in Tracker(Store(home / "tracker.db")).mapping_rows(
        Tracker(Store(home / "tracker.db")).portfolio(ACCOUNT)) if r.ticker == "NVO").symbol == "NVO.MX"
    assert not again.exception


def test_the_download_can_be_postponed(gui):
    t, fake, home = gui
    at = new_app().run()
    widget(at, "checkbox", "map_download").uncheck().run()
    at = edit_mapping(at, t, "NVO", alias="NVO.MX")
    widget(at, "button", "map_save").click().run()
    assert t.settings()["yahoo_symbols"] == {"NVO|USD": "NVO.MX"} and fake.calls == []


def test_an_invalid_alias_is_refused_in_the_page(gui):
    t, fake, home = gui
    at = edit_mapping(new_app().run(), t, "NVO", alias="NVO MX")
    widget(at, "button", "map_save").click().run()
    assert not at.exception and t.settings()["yahoo_symbols"] == {} and fake.calls == []
    reported = texts(at.error) + texts(at.warning) + texts(at.success) + texts(at.info)
    assert any("NVO: 'NVO MX' rejected - a symbol must not contain blanks" in x for x in reported), reported


def test_a_stock_can_be_set_to_manual_prices_only(gui):
    t, fake, home = gui
    at = edit_mapping(new_app().run(), t, "NVO", manual=True)
    widget(at, "button", "map_save").click().run()
    assert not at.exception and t.settings()["manual_only"] == ["NVO|USD"]
    sidebar_widget(at, "button", "refresh").click().run()
    assert "NVO" not in [c[0] for c in fake.calls]                                    # never downloaded


def test_testing_a_symbol_and_using_it(gui):
    t, fake, home = gui
    at = new_app().run()
    widget(at, "selectbox", "map_test_stock").set_value("NVO (USD)").run()
    widget(at, "text_input", "map_test_symbol").set_value("nvo.mx").run()
    widget(at, "button", "map_test").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any(s.value.startswith("NVO.MX: last close") and s.value.endswith("Fake NVO.MX") for s in at.success)
    assert t.settings()["yahoo_symbols"] == {}                                        # testing saves nothing
    widget(at, "button", "map_use").click().run()
    assert not at.exception and t.settings()["yahoo_symbols"] == {"NVO|USD": "NVO.MX"}
    bad = new_app().run()
    widget(bad, "text_input", "map_test_symbol").set_value("NVO MX").run()
    widget(bad, "button", "map_test").click().run()
    assert any(e.value.startswith("Not a valid symbol") for e in bad.error)


def test_symbol_testing_is_off_in_the_offline_mode(gui):
    t, fake, home = gui
    t.update_settings(price_source="statement")
    at = new_app().run()
    assert widget(at, "button", "map_test").disabled and widget(at, "checkbox", "map_download").disabled
    assert "Offline mode (statements only) is on - symbols cannot be tested." in captions(at)


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
