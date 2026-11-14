"""Portfolio analysis through the Tracker: the sample account, the settings, the CSV exports."""
from __future__ import annotations

import io
import json

import pandas as pd
import pytest

from fakes import BASE_PROFILES, FakeProvider
from synth import make_csv, make_usd_csv
from ibkr_tracker import allocation as A
from ibkr_tracker import analytics as an
from ibkr_tracker import exports
from ibkr_tracker.portfolio import CONSOLIDATED
from ibkr_tracker.storage import Store
from ibkr_tracker.tracker import Tracker

ACCOUNT = "U24013787"
SAMPLE_SECTORS = {"AMZN": "Consumer Cyclical", "BN": "Financial Services", "GOOGL": "Communication Services",
                  "META": "Communication Services", "MSFT": "Technology", "NVO": "Healthcare"}


def values(snap) -> dict[str, float]:
    return {r.ticker: r.usd_value for r in snap.metrics.positions}


def test_the_statement_only_sample_is_allocated_by_type_but_not_by_sector(loaded):
    snap = loaded.snapshot(ACCOUNT)
    v = values(snap)
    stocks, cash = sum(v.values()), snap.metrics.cash_usd
    assert snap.metrics.usd_total == pytest.approx(9641.103898, abs=1e-5)                 # the regression figure
    for by in A.GROUPINGS:
        assert snap.allocation(by).total == pytest.approx(snap.metrics.usd_total)
    cls = snap.allocation("asset_class")
    assert {s.label: s.usd_value for s in cls.slices} == pytest.approx({A.STOCKS: stocks, A.CASH: cash})
    kind = snap.allocation("stock_type")
    assert kind.get("ADR (depositary receipt)").usd_value == pytest.approx(v["NVO"])
    assert kind.get("Common stock").usd_value == pytest.approx(stocks - v["NVO"])
    sec = snap.allocation("sector")
    assert sec.get(A.UNCLASSIFIED).usd_value == pytest.approx(stocks) and sec.unclassified_share == pytest.approx(stocks / (stocks + cash))
    hold = snap.allocation("holding")
    assert hold.slices[0].label == "META" and hold.slices[0].usd_value == pytest.approx(max(v.values()))
    ccy = snap.allocation("currency")
    assert ccy.get("USD") is not None and ccy.get("HUF").usd_value == pytest.approx(cash)  # the only cash is HUF


def test_the_sector_view_after_a_lookup(loaded):
    out = loaded.lookup_profiles(ACCOUNT)
    assert out.ok and out.report.answered == 6 and "6 sector(s) found" in out.messages[0]
    snap = loaded.snapshot(ACCOUNT)
    v = values(snap)
    expected: dict[str, float] = {}
    for ticker, sector in SAMPLE_SECTORS.items():
        expected[sector] = expected.get(sector, 0.0) + v[ticker]
    expected[A.CASH] = snap.metrics.cash_usd
    sec = snap.allocation("sector")
    assert {s.label: s.usd_value for s in sec.slices} == pytest.approx(expected)
    assert sec.unclassified_share == 0.0 and sec.get("Communication Services").members == ["META", "GOOGL"]
    assert sum(s.weight for s in sec.slices) == pytest.approx(1.0)
    assert snap.holdings()[0].industry                                                      # the industry comes along
    again = loaded.lookup_profiles(ACCOUNT)                                                 # nothing left to ask
    assert len(again.report.cached) == 6 and again.report.answered == 0 and "6 already known" in again.messages[0]


def test_a_lookup_never_happens_by_itself(loaded, fake):
    loaded.refresh_prices(ACCOUNT)
    assert fake.profile_calls == [] and loaded.service.store.get_profiles() == {}
    loaded.snapshot(ACCOUNT).allocation("sector")
    assert fake.profile_calls == []


def test_offline_mode_does_not_look_up_sectors(loaded, fake):
    loaded.update_settings(price_source="statement")
    out = loaded.lookup_profiles(ACCOUNT)
    assert out.report.offline and fake.profile_calls == [] and "statements only" in out.messages[0]


def test_lookup_without_statements(tracker):
    out = tracker.lookup_profiles("U0000000")
    assert not out.ok and out.errors == ["No statements imported yet."]


def test_a_classification_is_saved_and_survives_a_restart(loaded, home):
    loaded.lookup_profiles(ACCOUNT)
    snap = loaded.snapshot(ACCOUNT)
    change = loaded.save_classification(snap.holdings(), {"NVO|USD": (A.STOCKS, "biotech"), "MSFT|USD": (A.STOCKS, "Technology"),
                                                          "META|USD": ("Other", "")})
    assert change.changed == ["NVO|USD", "META|USD"] and change.errors == []
    assert loaded.settings()["classification"] == {"NVO|USD": {"sector": "biotech"}, "META|USD": {"asset_class": "Other"}}
    fresh = Tracker(Store(home / "tracker.db"), FakeProvider())                              # a new process
    snap2 = fresh.snapshot(ACCOUNT)
    assert snap2.allocation("sector").get("biotech").members == ["NVO"]
    assert snap2.allocation("asset_class").get("Other").members == ["META"]
    unchanged = fresh.save_classification(snap2.holdings(), {"NVO|USD": (A.STOCKS, "biotech")})
    assert unchanged.changed == []
    cleared = fresh.save_classification(snap2.holdings(), {"NVO|USD": (A.STOCKS, ""), "META|USD": ("", "")})
    assert cleared.classification == {} and fresh.settings()["classification"] == {}
    assert fresh.snapshot(ACCOUNT).allocation("sector").get("Healthcare") is not None        # back to Yahoo's sector


def test_invalid_entries_are_reported_and_not_saved(loaded):
    snap = loaded.snapshot(ACCOUNT)
    change = loaded.save_classification(snap.holdings(), {"NVO|USD": (A.STOCKS, "z" * 99)})
    assert change.errors and not change.changed and loaded.settings()["classification"] == {}


def test_risk_needs_daily_prices(loaded):
    before = loaded.snapshot(ACCOUNT).risk("usd")
    assert before.status == an.NO_PRICES and "Daily prices are missing" in before.message and before.rows() == []
    loaded.refresh_prices(ACCOUNT)
    snap = loaded.snapshot(ACCOUNT)
    after, base = snap.risk("usd"), snap.risk("base")
    assert after.ok and base.ok and after.currency == "USD" and base.currency == "HUF" and after.n_obs > 150
    assert after.portfolio.volatility > 0 and after.portfolio.max_drawdown <= 0 and after.relative is not None
    assert after.benchmark_symbol == "VUAA.L" and after.price_coverage == pytest.approx(1.0)
    last = snap.history().frame
    assert after.end == last.index[-1].date() and after.start == snap.history().start


def test_risk_windows(loaded):
    loaded.refresh_prices(ACCOUNT)
    snap = loaded.snapshot(ACCOUNT)
    whole = snap.risk("usd")
    quarter = snap.risk("usd", since=snap.history().end.replace(month=8, day=1))
    assert quarter.start > whole.start and quarter.n_obs < whole.n_obs and quarter.status in (an.OK, an.FEW_DAYS)
    assert snap.risk("usd") is whole                                                           # computed once per snapshot


def test_the_risk_free_rate_is_remembered_per_currency(loaded):
    loaded.refresh_prices(ACCOUNT)
    zero = loaded.snapshot(ACCOUNT).risk("usd")
    assert zero.risk_free == 0.0
    loaded.set_risk_free("USD", 4.0)
    loaded.set_risk_free("HUF", 6.5)
    assert loaded.settings()["risk_free"] == {"USD": 4.0, "HUF": 6.5}
    snap = loaded.snapshot(ACCOUNT)
    usd, huf = snap.risk("usd"), snap.risk("base")
    assert usd.risk_free == pytest.approx(0.04) and huf.risk_free == pytest.approx(0.065)
    assert usd.portfolio.sharpe < zero.portfolio.sharpe and usd.portfolio.volatility == zero.portfolio.volatility
    assert snap.risk("usd", risk_free=0.0).portfolio.sharpe == pytest.approx(zero.portfolio.sharpe)    # an explicit rate wins


def test_new_settings_default_to_empty_for_an_old_database(home):
    store = Store(home / "old.db")
    assert store.get_settings()["classification"] == {} and store.get_settings()["risk_free"] == {}
    store.kv_set("settings", json.dumps({"hide_closed_dust": True, "price_source": "statement"}))    # saved by version 1.3
    saved = store.get_settings()
    assert saved["classification"] == {} and saved["risk_free"] == {}
    assert saved["hide_closed_dust"] is True and saved["price_source"] == "statement"
    store.save_settings({**saved, "risk_free": {"USD": 3.5}})
    assert store.get_settings()["risk_free"] == {"USD": 3.5} and store.get_settings()["classification"] == {}


# ---------------------------------------------------------------------------------------------------------------
# CSV exports
# ---------------------------------------------------------------------------------------------------------------
def test_csv_tables_of_the_analysis(loaded):
    snap = loaded.snapshot(ACCOUNT)
    tables = exports.csv_tables(snap)
    assert {"allocation.csv", "holdings.csv"} <= set(tables) and not ({"risk_metrics.csv", "risk_daily.csv"} & set(tables))
    alloc = tables["allocation.csv"]
    assert set(alloc["Grouping"]) == set(A.GROUPINGS.values())
    for _, part in alloc.groupby("Grouping"):
        assert part["Weight"].sum() == pytest.approx(1.0) and part["USD Value"].sum() == pytest.approx(snap.metrics.usd_total)
    hold = tables["holdings.csv"]
    assert len(hold) == 7 and hold["Weight"].sum() == pytest.approx(1.0) and set(hold["Asset class"]) == {A.STOCKS, A.CASH}

    loaded.refresh_prices(ACCOUNT)
    snap = loaded.snapshot(ACCOUNT)
    tables = exports.csv_tables(snap)
    metrics, daily = tables["risk_metrics.csv"], tables["risk_daily.csv"]
    assert exports.risk_units(snap) == ["base", "usd"] and set(metrics["Currency"]) == {"HUF", "USD"} == set(daily["Currency"])
    vol = metrics[(metrics["Currency"] == "USD") & (metrics["Metric"] == "Annualised volatility")].iloc[0]
    assert vol["Portfolio"] == pytest.approx(snap.risk("usd").portfolio.volatility) and vol["Format"].startswith("percent")
    assert vol["Observation days"] == snap.risk("usd").n_obs and vol["Risk-free rate"] == 0.0
    usd_daily = daily[daily["Currency"] == "USD"]
    assert len(usd_daily) == snap.risk("usd").n_obs and usd_daily["Date"].is_monotonic_increasing


def test_csv_files_use_the_chosen_number_style(loaded):
    loaded.refresh_prices(ACCOUNT)
    snap = loaded.snapshot(ACCOUNT)
    intl, euro = exports.csv_files(snap, "international"), exports.csv_files(snap, "european")
    assert {"allocation.csv", "holdings.csv", "risk_metrics.csv", "risk_daily.csv"} <= set(intl) == set(euro)
    a = pd.read_csv(io.BytesIO(intl["allocation.csv"]))
    b = pd.read_csv(io.BytesIO(euro["allocation.csv"]), sep=";", decimal=",")
    assert a["Weight"].tolist() == pytest.approx(b["Weight"].tolist())
    risk = exports.risk_csv(snap, "usd", None, 0.02)
    assert risk.startswith(b"\xef\xbb\xbf") and b"Annualised volatility" in risk
    assert pd.read_csv(io.BytesIO(risk)).loc[0, "Risk-free rate"] == 0.02


def test_no_risk_csv_while_the_metrics_cannot_be_computed(loaded):
    snap = loaded.snapshot(ACCOUNT)                                                           # statements only: no daily prices
    assert snap.risk("usd").status == an.NO_PRICES
    assert exports.risk_csv(snap, "usd") is None and exports.risk_csv(snap, "base") is None
    assert exports.risk_units(snap) == ["base", "usd"]                                        # HUF account: HUF and USD blocks


def test_the_consolidated_view_is_allocated_across_the_accounts(tracker):
    for name, data in (("eur.csv", make_csv()[0]), ("usd.csv", make_usd_csv()[0])):
        assert tracker.import_statement(name, data).ok
    snap = tracker.snapshot(CONSOLIDATED)
    tickers = {h.ticker for h in snap.holdings() if not h.is_cash}
    assert tickers == {"AAPL", "SAP", "MSFT", "KO"}
    for by in A.GROUPINGS:
        assert snap.allocation(by).total == pytest.approx(snap.metrics.usd_total)
    assert {s.label for s in snap.allocation("currency").slices} == {"USD", "EUR"}
    tracker.lookup_profiles(CONSOLIDATED)
    sectors = {s.label: s.members for s in tracker.snapshot(CONSOLIDATED).allocation("sector").slices}
    assert sectors["Technology"] == ["AAPL", "MSFT", "SAP"] or sorted(sectors["Technology"]) == ["AAPL", "MSFT", "SAP"]
    assert "KO" in sectors["Consumer Defensive"]
    assert BASE_PROFILES["KO"][0] == "Consumer Defensive"
