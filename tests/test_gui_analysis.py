"""Streamlit GUI tests (``AppTest``) of the portfolio analysis section of the dashboard: the allocation donut chart, the
classification editor, the sector lookup and the risk & return metrics.  Yahoo Finance is replaced by the fake provider."""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Optional

import pytest

from fakes import BASE_PROFILES
from ibkr_tracker import allocation as A
from ibkr_tracker import analytics as an
from ibkr_tracker import tables as T
from ibkr_tracker.market.providers import YahooProvider
from test_gui import ACCOUNT, captions, gui, kpis, new_app, sidebar_widget, texts, widget   # noqa: F401 - gui is a fixture

SECTORS = {"AMZN": "Consumer Cyclical", "BN": "Financial Services", "GOOGL": "Communication Services",
           "META": "Communication Services", "MSFT": "Technology", "NVO": "Healthcare"}


# ---------------------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------------------
def _layers(node, out: Optional[list] = None) -> list[dict]:
    out = [] if out is None else out
    if isinstance(node, dict):
        if "mark" in node:
            out.append(node)
        for v in node.values():
            _layers(v, out)
    elif isinstance(node, list):
        for v in node:
            _layers(v, out)
    return out


def _mark(layer: dict) -> tuple[str, dict]:
    m = layer["mark"]
    return (m, {}) if isinstance(m, str) else (m.get("type", ""), m)


def donut(at) -> Optional[dict]:
    """The donut chart of the page: ``{"arc": <mark>, "legend": [...], "texts": [...]}`` - ``None`` without one."""
    for chart in at.get("vega_lite_chart"):
        layers = _layers(json.loads(chart.proto.spec))
        arcs = [l for l in layers if _mark(l)[0] == "arc"]
        if arcs:
            return {"arc": _mark(arcs[0])[1], "legend": arcs[0]["encoding"]["color"]["scale"]["domain"],
                    "texts": [l["encoding"]["text"]["value"] for l in layers if _mark(l)[0] == "text"]}
    return None


def expected_legend(alloc: A.Allocation) -> list[str]:
    return [f"{s.label}  ·  {s.weight * 100:.1f}%" for s in A.chart_slices(alloc)]


def frames_with(at, *columns: str) -> list:
    """Every table of the page that has all of the ``columns``."""
    return [d.value for d in at.dataframe if set(columns) <= set(d.value.columns)]


def frame_with(at, *columns: str):
    """The first table of the page that has all of the ``columns``."""
    found = frames_with(at, *columns)
    return found[0] if found else None


def set_group(at, key: str):
    widget(at, "radio", "alloc_by").set_value(key).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


# ---------------------------------------------------------------------------------------------------------------
# allocation
# ---------------------------------------------------------------------------------------------------------------
def test_the_allocation_donut_is_on_the_dashboard(gui):
    t, fake, home = gui
    at = new_app().run()
    assert not at.exception, [e.value for e in at.exception]
    labels = [x.label for x in at.tabs]
    assert labels.index("Allocation breakdown") + 1 == labels.index("Risk & return")
    assert any(m.value == "#### Portfolio analysis" for m in at.markdown)
    radio = widget(at, "radio", "alloc_by")
    assert radio.options == list(A.GROUPINGS.values()) and radio.value == "asset_class"
    assert radio.options == ["Asset class", "Stock type", "Industry sector", "Currency", "Holding"]
    chart = donut(at)
    assert chart is not None and chart["arc"]["innerRadius"] > 0 and chart["arc"]["outerRadius"] > chart["arc"]["innerRadius"]
    snap = t.snapshot(ACCOUNT)
    alloc = snap.allocation("asset_class")
    assert chart["legend"] == expected_legend(alloc) and chart["legend"][0].startswith("Stocks  ·  99.")
    assert chart["texts"] == [f"{alloc.total:,.0f}", "USD in total"]                       # the hole: the total
    table = frame_with(at, "Asset class", "USD Value", "Weight", "Holdings")
    assert table is not None and "Members" not in table.columns
    assert table["Asset class"].tolist() == [s.label for s in alloc.slices]
    assert table["Weight"].sum() == pytest.approx(100.0)                                     # shown in percent
    assert f"Total {alloc.total:,.2f} USD = 100 %." in captions(at)


def test_every_grouping_has_a_donut(gui):
    t, fake, home = gui
    snap = t.snapshot(ACCOUNT)
    at = new_app().run()
    for key, label in A.GROUPINGS.items():
        set_group(at, key)
        chart = donut(at)
        assert chart is not None, key
        assert chart["legend"] == expected_legend(snap.allocation(key)), key
        assert frame_with(at, label, "USD Value", "Weight") is not None, key
    # stock types of the sample: five common stocks and the Novo Nordisk ADR
    set_group(at, "stock_type")
    assert {leg.split("  ·  ")[0] for leg in donut(at)["legend"]} == {"Common stock", "ADR (depositary receipt)", "Cash"}
    set_group(at, "currency")
    assert {leg.split("  ·  ")[0] for leg in donut(at)["legend"]} == {"USD", "HUF"}
    set_group(at, "holding")
    legend = donut(at)["legend"]
    assert len(legend) == 7 and legend[0].startswith("META  ·  ")                          # the largest position first


def test_with_few_slices_nothing_is_merged_into_other(gui):
    t, fake, home = gui
    at = new_app().run()
    set_group(at, "holding")
    snap = t.snapshot(ACCOUNT)
    n = len(snap.allocation("holding").slices)
    assert n == 7 and len(donut(at)["legend"]) == n                                          # <= 10: nothing is merged
    assert not any("merged into *Other*" in c.value for c in at.caption)


def test_the_sector_view_asks_for_a_lookup_first(gui):
    t, fake, home = gui
    at = new_app().run()
    set_group(at, "sector")
    assert donut(at)["legend"][0].startswith("Unclassified  ·  99.")
    assert any("has no sector yet" in i.value and "Look up sectors" in i.value for i in at.info)
    assert fake.profile_calls == []                                                          # nothing was asked by itself
    t.refresh_prices(ACCOUNT)                                                                # ... not even by a refresh
    assert fake.profile_calls == []


def test_concentration_cards(gui):
    t, fake, home = gui
    at = new_app().run()
    k = kpis(at)
    snap = t.snapshot(ACCOUNT)
    c = A.concentration(snap.holdings())
    assert k["Stock positions"] == f"{c.n_positions:,.0f}" == "6"
    assert k["Largest position (META)"] == f"{c.largest_weight * 100:.1f}%"
    assert k["Top 3 positions"] == f"{c.top3_weight * 100:.1f}%" and k["Top 5 positions"] == f"{c.top5_weight * 100:.1f}%"
    assert k["Effective positions"] == f"{c.effective_positions:,.2f}"
    assert 1.0 < c.effective_positions < 6.0


# ---------------------------------------------------------------------------------------------------------------
# sector lookup and classification
# ---------------------------------------------------------------------------------------------------------------
@pytest.fixture
def lookup(gui, monkeypatch):
    t, fake, home = gui
    monkeypatch.setattr(YahooProvider, "profile", lambda self, symbol: fake.profile(symbol))
    return t, fake, home


def test_the_lookup_button_fills_the_sector_donut(lookup):
    t, fake, home = lookup
    at = new_app().run()
    set_group(at, "sector")
    widget(at, "button", "class_lookup").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("6 sector(s) found" in s.value for s in at.success), texts(at.success) + texts(at.warning)
    assert sorted(fake.profile_calls) == sorted(["AMZN", "BN", "GOOGL", "META", "MSFT", "NVO"])
    snap = t.snapshot(ACCOUNT)
    assert donut(at)["legend"] == expected_legend(snap.allocation("sector"))
    assert {leg.split("  ·  ")[0] for leg in donut(at)["legend"]} == {*SECTORS.values(), "Cash"}
    assert not any("has no sector yet" in i.value for i in at.info)
    table = frame_with(at, "Industry sector", "USD Value", "Weight", "Holdings")
    assert table["Holdings"].sum() == 7 and set(table["Industry sector"]) == {*SECTORS.values(), "Cash"}
    assert t.service.store.get_profiles().keys() == {f"{k}|USD" for k in SECTORS}              # remembered
    fake.profile_calls.clear()
    widget(at, "button", "class_lookup").click().run()                                       # asking again: all known
    assert not at.exception and fake.profile_calls == []
    messages = texts(at.success) + texts(at.warning) + texts(at.info)
    assert any("6 already known" in m for m in messages), messages


def test_the_lookup_reports_a_stock_yahoo_does_not_know(lookup):
    t, fake, home = lookup
    fake.profiles = {k: v for k, v in fake.profiles.items() if k != "BN"}
    at = new_app().run()
    widget(at, "button", "class_lookup").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("BN" in w.value for w in at.warning), texts(at.warning)
    set_group(at, "sector")
    assert any(leg.startswith("Unclassified  ·  ") for leg in donut(at)["legend"])             # BN stays unclassified


def test_a_yahoo_outage_during_the_lookup_is_reported_not_raised(lookup):
    t, fake, home = lookup
    fake.profile_fail = set(SECTORS)
    at = new_app().run()
    widget(at, "button", "class_lookup").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("simulated outage" in w.value or "could not" in w.value.lower() for w in at.warning), texts(at.warning)
    assert t.service.store.get_profiles() == {}


def test_the_lookup_is_off_in_the_offline_mode(lookup):
    t, fake, home = lookup
    at = new_app().run()
    sidebar_widget(at, "selectbox", "w_source").set_value("statement").run()
    assert not at.exception
    assert widget(at, "button", "class_lookup").disabled and widget(at, "checkbox", "class_force").disabled
    assert "Offline mode (statements only) is on - sectors cannot be looked up" in captions(at)
    assert fake.profile_calls == []


def edit_classification(at, edits: dict[str, dict[str, str]], holdings) -> None:
    """Edit the classification table like a user: ``edits`` = ``{ticker: {column: value}}``."""
    df = T.classification_table(holdings)
    rows = {int(df.index[df["Ticker"] == tk][0]): cols for tk, cols in edits.items()}
    nonce = at.session_state["class_nonce"] if "class_nonce" in at.session_state else 0
    at.session_state[f"class_editor_{nonce}"] = {"edited_rows": rows, "added_rows": [], "deleted_rows": []}


def test_a_classification_is_saved_from_the_editor(lookup):
    t, fake, home = lookup
    at = new_app().run()
    holdings = t.snapshot(ACCOUNT).holdings()
    edit_classification(at, {"NVO": {"Sector": "Biotech"}, "META": {"Asset class": "Other"}}, holdings)
    widget(at, "button", "class_save").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("Classification saved (2 holding(s) changed)." in s.value for s in at.success), texts(at.success) + texts(at.info)
    assert t.settings()["classification"] == {"NVO|USD": {"sector": "Biotech"}, "META|USD": {"asset_class": "Other"}}
    set_group(at, "sector")
    assert any(leg.startswith("Biotech  ·  ") for leg in donut(at)["legend"])
    set_group(at, "asset_class")
    assert any(leg.startswith("Other  ·  ") for leg in donut(at)["legend"])
    again = new_app().run()                                                                  # a new browser session
    set_group(again, "sector")
    assert any(leg.startswith("Biotech  ·  ") for leg in donut(again)["legend"])


def test_saving_without_a_change_says_so(lookup):
    t, fake, home = lookup
    at = new_app().run()
    widget(at, "button", "class_save").click().run()
    assert not at.exception and any("The classification is unchanged." in i.value for i in at.info)
    assert t.settings()["classification"] == {}


# ---------------------------------------------------------------------------------------------------------------
# risk & return
# ---------------------------------------------------------------------------------------------------------------
RISK_KPIS = ["Annualised volatility", "Maximum drawdown", "Sharpe ratio", "Sortino ratio", "Time-weighted return",
             "Calmar ratio", "Beta vs VUAA", "Daily Value at Risk (95%)"]


def test_the_risk_tab_waits_for_daily_prices(gui):
    t, fake, home = gui
    at = new_app().run()
    assert not at.exception
    assert any("Daily prices are missing" in i.value for i in at.info), texts(at.info)
    k = kpis(at)
    assert not any(name in k for name in RISK_KPIS)
    assert "⬇  Download these risk metrics (CSV)" not in [b.proto.label for b in at.get("download_button")]
    assert [r.options for r in at.radio if r.key == "risk_unit"] == [["HUF", "USD"]]
    assert widget(at, "number_input", "risk_rf_HUF").value == 0.0


def test_the_risk_metrics_after_a_refresh(gui):
    t, fake, home = gui
    at = new_app().run()
    sidebar_widget(at, "button", "refresh").click().run()
    assert not at.exception, [e.value for e in at.exception]
    snap = t.snapshot(ACCOUNT)
    rep = snap.risk("base")
    assert rep.ok and rep.currency == "HUF"
    k = kpis(at)
    for name in RISK_KPIS:
        assert name in k, name
    assert k["Annualised volatility"] == f"{rep.portfolio.volatility * 100:.2f}%"
    assert k["Maximum drawdown"] == f"{rep.portfolio.max_drawdown * 100:.2f}%"
    assert k["Sharpe ratio"] == f"{rep.portfolio.sharpe:.2f}" and k["Sortino ratio"] == f"{rep.portfolio.sortino:.2f}"
    assert k["Time-weighted return"] == f"{rep.portfolio.cumulative * 100:+.2f}%"
    assert k["Beta vs VUAA"] == f"{rep.relative.beta:.2f}"
    assert not any("Daily prices are missing" in i.value for i in at.info)
    names = [x.label for x in at.tabs]
    assert names[names.index("Risk & return") + 1:][:3] == ["Drawdown", "Rolling volatility (30 days)", "Return distribution"]
    shown = [m for table in frames_with(at, "Metric", "Portfolio", "What it means") for m in table["Metric"]]
    assert shown == [r.label for r in rep.rows() if r.label != an.DRAWDOWN_PERIOD_LABEL] and len(shown) >= 15
    assert {"Annualised volatility", "Maximum drawdown", "Sharpe ratio", "Sortino ratio", "Calmar ratio", "Beta", "Alpha (Jensen, annualised)",
            "Daily Value at Risk (95 %)", "Tracking error (annualised)", "Information ratio"} <= set(shown)
    assert "⬇  Download these risk metrics (CSV)" in [b.proto.label for b in at.get("download_button")]
    assert "Annualised with 252 trading days." in captions(at)


def test_the_risk_currency_and_period_controls_rerun_cleanly(gui):
    t, fake, home = gui
    at = new_app().run()
    sidebar_widget(at, "button", "refresh").click().run()
    unit = next(r for r in at.radio if r.key == "risk_unit")
    period = next(r for r in at.radio if r.key == "risk_period")
    assert unit.options == ["HUF", "USD"] and period.options == ["All", "1Y", "YTD", "6M", "3M", "1M"]
    snap = t.snapshot(ACCOUNT)
    for u, ccy in (("USD", "USD"), ("HUF", "HUF")):
        next(r for r in at.radio if r.key == "risk_unit").set_value(u).run()
        for per in ("1M", "YTD", "3M", "All"):
            next(r for r in at.radio if r.key == "risk_period").set_value(per).run()
            assert not at.exception, (u, per, [e.value for e in at.exception])
        rep = snap.risk("usd" if ccy == "USD" else "base")
        assert kpis(at)["Annualised volatility"] == f"{rep.portfolio.volatility * 100:.2f}%"      # "All" again
        assert widget(at, "number_input", f"risk_rf_{ccy}")                                      # one rate per currency


def test_the_risk_free_rate_is_remembered_per_currency(gui):
    t, fake, home = gui
    at = new_app().run()
    sidebar_widget(at, "button", "refresh").click().run()
    before = kpis(at)["Sharpe ratio"]
    widget(at, "number_input", "risk_rf_HUF").set_value(6.5).run()
    assert not at.exception, [e.value for e in at.exception]
    assert t.settings()["risk_free"] == {"HUF": 6.5}
    assert kpis(at)["Sharpe ratio"] != before
    assert kpis(at)["Sharpe ratio"] == f"{t.snapshot(ACCOUNT).risk('base').portfolio.sharpe:.2f}"
    next(r for r in at.radio if r.key == "risk_unit").set_value("USD").run()
    assert widget(at, "number_input", "risk_rf_USD").value == 0.0                              # USD has its own rate
    widget(at, "number_input", "risk_rf_USD").set_value(4.0).run()
    assert t.settings()["risk_free"] == {"HUF": 6.5, "USD": 4.0}
    again = new_app().run()                                                                  # a new browser session
    assert widget(again, "number_input", "risk_rf_HUF").value == 6.5
    assert kpis(again)["Sharpe ratio"] == f"{t.snapshot(ACCOUNT).risk('base').portfolio.sharpe:.2f}"


def test_a_short_window_is_marked_indicative(gui):
    t, fake, home = gui
    at = new_app().run()
    sidebar_widget(at, "button", "refresh").click().run()
    assert not any("indicative" in w.value for w in at.warning)                                # the whole lifespan: 200+ days
    next(r for r in at.radio if r.key == "risk_period").set_value("1M").run()
    assert not at.exception, [e.value for e in at.exception]
    rep = t.snapshot(ACCOUNT).risk("base", since=date(2026, 10, 30) - timedelta(days=30))
    assert rep.status == an.OK and 20 <= rep.n_obs < 60                                       # enough days for the ratios ...
    assert any("indicative" in w.value for w in at.warning), texts(at.warning)                 # ... but not for much trust
    assert kpis(at)["Sharpe ratio"] == f"{rep.portfolio.sharpe:.2f}"


def test_the_analysis_follows_the_consolidated_view(gui):
    from synth import make_usd_csv
    from ibkr_tracker.portfolio import CONSOLIDATED
    t, fake, home = gui
    assert t.import_statement("usd.csv", make_usd_csv()[0]).ok
    at = new_app().run()
    sidebar_widget(at, "selectbox", "account").set_value(CONSOLIDATED).run()
    assert not at.exception, [e.value for e in at.exception]
    snap = t.snapshot(CONSOLIDATED)
    assert donut(at)["legend"] == expected_legend(snap.allocation("asset_class"))
    set_group(at, "currency")
    assert {leg.split("  ·  ")[0] for leg in donut(at)["legend"]} >= {"USD"}
    assert len(snap.holdings()) > 7
