"""Market data: provider parsing, symbol mapping, incremental refresh, priority rules, offline fallback."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import TODAY
from fakes import FakeProvider
from ibkr_tracker.config import DEFAULT_SETTINGS
from ibkr_tracker.market import MarketDataService
from ibkr_tracker.market.fallback import statement_fx, statement_prices
from ibkr_tracker.market.providers import (ProviderError, YahooProvider, drop_incomplete_bar, normalise_currency,
                                           parse_chart_json)
from ibkr_tracker.market.symbols import yahoo_symbol
from ibkr_tracker.excel.builder import MissingMarketData, build_workbook


# ======================================================================================================
# provider helpers (pure functions)
# ======================================================================================================
def _payload(closes, start=datetime(2026, 10, 26, 14, 30, tzinfo=timezone.utc), currency="USD", gmtoffset=-14400,
             session_end=None):
    stamps = [int((start + timedelta(days=i)).timestamp()) for i in range(len(closes))]
    meta = {"currency": currency, "gmtoffset": gmtoffset, "longName": "Test Corp"}
    if session_end is not None:
        meta["currentTradingPeriod"] = {"regular": {"end": session_end}}
    return {"chart": {"result": [{"meta": meta, "timestamp": stamps,
                                  "indicators": {"quote": [{"close": closes}]}}], "error": None}}


def test_parse_chart_json_basic():
    res = parse_chart_json("TEST", _payload([10.0, None, 12.5]))
    assert res.rows == [(date(2026, 10, 26), 10.0), (date(2026, 10, 28), 12.5)]       # null close skipped
    assert (res.currency, res.name) == ("USD", "Test Corp")


def test_parse_chart_json_minor_units_become_major_units():
    res = parse_chart_json("VOD.L", _payload([7250.0, 7300.0], currency="GBp"))
    assert res.currency == "GBP" and res.rows[0][1] == pytest.approx(72.5)
    assert normalise_currency("GBp") == ("GBP", 0.01) and normalise_currency("eur") == ("EUR", 1.0)
    assert normalise_currency(None) == (None, 1.0)


def test_parse_chart_json_errors():
    err = {"chart": {"result": None, "error": {"code": "Not Found", "description": "No data found, symbol may be delisted"}}}
    with pytest.raises(ProviderError, match="Not Found"):
        parse_chart_json("XXX", err)
    with pytest.raises(ProviderError):
        parse_chart_json("XXX", {"chart": {"result": [], "error": None}})
    with pytest.raises(ProviderError):
        parse_chart_json("XXX", _payload([None, None]))


def test_todays_incomplete_bar_is_dropped_only_while_the_market_is_open():
    last_day = datetime(2026, 10, 30, 14, 30, tzinfo=timezone.utc)
    end = datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc).timestamp()            # session end 20:00 UTC
    payload = _payload([1.0, 2.0, 3.0], start=last_day - timedelta(days=2), session_end=end)
    during = parse_chart_json("T", payload, now_utc=datetime(2026, 10, 30, 17, 0, tzinfo=timezone.utc))
    after = parse_chart_json("T", payload, now_utc=datetime(2026, 10, 30, 21, 0, tzinfo=timezone.utc))
    keep = parse_chart_json("T", payload, now_utc=datetime(2026, 10, 30, 17, 0, tzinfo=timezone.utc), include_intraday=True)
    assert [d for d, _ in during.rows][-1] == date(2026, 10, 29)
    assert [d for d, _ in after.rows][-1] == date(2026, 10, 30)
    assert [d for d, _ in keep.rows][-1] == date(2026, 10, 30)
    assert drop_incomplete_bar([], None, 0, datetime.now(timezone.utc)) == []


def test_yahoo_provider_falls_back_to_the_chart_api(monkeypatch):
    import requests

    calls = []

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._body = status, body

        def json(self):
            return self._body

    answers = iter([Resp(429, {}), Resp(200, _payload([5.0, 6.0]))])

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        return next(answers)

    monkeypatch.setattr(requests, "get", fake_get)
    prov = YahooProvider(retries=2, pause=0.0)
    monkeypatch.setattr(prov, "_via_yfinance", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("yfinance down")))
    res = prov.history("ABC", date(2026, 10, 1), date(2026, 10, 30), include_intraday=True)
    assert [v for _, v in res.rows] == [5.0, 6.0] and len(calls) == 2         # rate limit -> retry -> success
    prov2 = YahooProvider(retries=1, pause=0.0)
    monkeypatch.setattr(prov2, "_via_yfinance", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("yfinance down")))
    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp(500, {}))
    with pytest.raises(ProviderError) as ei:
        prov2.history("ABC", date(2026, 10, 1), date(2026, 10, 30))
    assert "yfinance down" in str(ei.value) and "chart API" in str(ei.value)       # both failures are reported


def test_symbol_mapping():
    assert yahoo_symbol("AAPL", "NASDAQ", "USD") == "AAPL"
    assert yahoo_symbol("SAP", "IBIS", "EUR") == "SAP.DE"
    assert yahoo_symbol("BRK B", "NYSE", "USD") == "BRK-B"
    assert yahoo_symbol("700", "SEHK", "HKD") == "0700.HK"
    assert yahoo_symbol("OTP", "BUX", "HUF") == "OTP.BD"
    assert yahoo_symbol("VUAA", "LSEETF", "USD") == "VUAA.L"
    assert yahoo_symbol("X", "UNKNOWNEXCH", "USD") == "X"
    assert yahoo_symbol("NVO", "NYSE", "USD", {"NVO|USD": "NOVO-B.CO"}) == "NOVO-B.CO"      # per-currency override
    assert yahoo_symbol("NVO", "NYSE", "USD", {"NVO": "NVO2"}) == "NVO2"                    # ticker-only override


# ======================================================================================================
# statement-derived fallback
# ======================================================================================================
def test_statement_prices_are_the_period_end_closes(pf_csv):
    px = statement_prices(pf_csv)
    assert set(px) == {f"{t}|USD" for t in ("AMZN", "BN", "GOOGL", "META", "MSFT", "NVO")}
    assert px["AMZN|USD"].price == 248.0 and px["AMZN|USD"].day == date(2026, 10, 23)
    assert px["META|USD"].price == 642.62 and px["NVO|USD"].price == 39.05


def test_statement_fx_uses_ibkr_closing_rates(pf_csv):
    fx = statement_fx(pf_csv)
    huf = fx["HUF"]
    assert huf.last == (327.38, date(2026, 10, 23))                  # MtM period-end close of the statement
    assert huf.asof(date(2026, 1, 29)) == pytest.approx(317.7034713)  # the 17:00:00 sweep trade = IBKR closing rate
    assert huf.asof(date(2026, 1, 1)) == huf.rates[0]                # before the first rate: first row
    assert huf.dates == sorted(huf.dates)


# ======================================================================================================
# refresh (network replaced by the deterministic fake)
# ======================================================================================================
def test_first_refresh_downloads_everything_from_before_the_first_activity(store, pf_csv):
    prov = FakeProvider(last_day=TODAY)
    svc = MarketDataService(store, prov, today=lambda: TODAY)
    rep = svc.refresh(pf_csv, DEFAULT_SETTINGS)
    expected = {"AMZN", "BN", "GOOGL", "META", "MSFT", "NVO", "HUF=X", "EUR=X", "VUAA.L"}
    assert set(rep.fetched) == expected and not rep.errors and not rep.offline
    assert rep.summary() == "Updated 9 symbol(s)."
    first_activity = pf_csv.first_activity()
    assert first_activity == date(2026, 1, 14)
    assert {c[1] for c in prov.calls} == {first_activity - timedelta(days=14)}
    assert {c[2] for c in prov.calls} == {TODAY}
    assert store.last_price_date("AMZN") == TODAY and store.first_price_date("AMZN") >= date(2025, 12, 31)
    assert store.kv_get("last_refresh")


def test_second_refresh_is_incremental(store, pf_csv):
    prov = FakeProvider(last_day=TODAY)
    svc = MarketDataService(store, prov, today=lambda: TODAY)
    svc.refresh(pf_csv, DEFAULT_SETTINGS)
    rows_before = {s: len(store.get_prices(s)) for s in ("AMZN", "HUF=X", "VUAA.L")}
    prov.calls.clear()
    next_day = date(2026, 11, 2)                                      # Monday after "today"
    prov.last_day = next_day
    svc2 = MarketDataService(store, prov, today=lambda: next_day)
    svc2.refresh(pf_csv, DEFAULT_SETTINGS)
    assert {c[1] for c in prov.calls} == {TODAY - timedelta(days=7)}      # only a week of overlap is re-read
    for s, n in rows_before.items():
        assert len(store.get_prices(s)) == n + 1                      # exactly one new trading day, no duplicates
        assert store.last_price_date(s) == next_day


def test_cache_that_starts_too_late_is_refilled_from_the_start(store, pf_csv):
    store.upsert_prices("AMZN", [(date(2026, 9, 1), 100.0)], "USD", "x")
    prov = FakeProvider(last_day=TODAY)
    MarketDataService(store, prov, today=lambda: TODAY).refresh(pf_csv, DEFAULT_SETTINGS)
    amzn = [c for c in prov.calls if c[0] == "AMZN"][0]
    assert amzn[1] == date(2026, 1, 14) - timedelta(days=14)


def test_one_failing_symbol_does_not_stop_the_others(store, pf_csv):
    prov = FakeProvider(fail=("AMZN",), last_day=TODAY)
    svc = MarketDataService(store, prov, today=lambda: TODAY)
    rep = svc.refresh(pf_csv, DEFAULT_SETTINGS)
    assert set(rep.errors) == {"AMZN"} and len(rep.fetched) == 8 and not rep.aborted
    assert "failed" in rep.summary()
    assert store.price_meta("AMZN")["last_error"]
    md = svc.build(pf_csv, DEFAULT_SETTINGS)
    assert md.prices["AMZN|USD"].source.startswith("IBKR statement")        # fell back to the statement price
    assert md.prices["MSFT|USD"].source == "Yahoo Finance"


def test_refresh_gives_up_quickly_when_yahoo_is_unreachable(store, pf_csv):
    syms = ("AMZN", "BN", "GOOGL", "META", "MSFT", "NVO", "HUF=X", "EUR=X", "VUAA.L")
    prov = FakeProvider(fail=syms)
    rep = MarketDataService(store, prov, today=lambda: TODAY).refresh(pf_csv, DEFAULT_SETTINGS)
    assert rep.aborted and len(rep.errors) == 3 and len(rep.skipped) == 6 and len(prov.calls) == 3
    assert "could not be reached" in rep.messages[0]
    assert not rep.fetched and rep.summary().endswith("6 skipped.")


def test_failing_stocks_do_not_make_the_refresh_give_up(store, pf_csv):
    """Three stocks fail in a row (a naming problem at Yahoo) - but the FX pairs and the benchmark are fetched first
    and arrive, so Yahoo is evidently reachable: the refresh goes on with the rest."""
    prov = FakeProvider(fail=("AMZN", "BN", "GOOGL"), last_day=TODAY)
    rep = MarketDataService(store, prov, today=lambda: TODAY).refresh(pf_csv, DEFAULT_SETTINGS)
    assert not rep.aborted and set(rep.errors) == {"AMZN", "BN", "GOOGL"} and len(rep.fetched) == 6
    assert [c[0] for c in prov.calls[:3]] == ["HUF=X", "EUR=X", "VUAA.L"]           # sentinels before the stocks
    prov2 = FakeProvider(fail=("META", "MSFT", "NVO"), last_day=TODAY)
    rep2 = MarketDataService(store, prov2, today=lambda: TODAY).refresh(pf_csv, DEFAULT_SETTINGS)
    assert not rep2.aborted and len(rep2.errors) == 3 and len(rep2.fetched) == 6


def test_refresh_only_downloads_the_requested_symbols(store, pf_csv):
    prov = FakeProvider(last_day=TODAY)
    rep = MarketDataService(store, prov, today=lambda: TODAY).refresh(pf_csv, DEFAULT_SETTINGS, only=["AMZN", "META"])
    assert {c[0] for c in prov.calls} == {"AMZN", "META"} and set(rep.fetched) == {"AMZN", "META"}


def test_manual_only_stocks_are_never_downloaded(store, pf_csv):
    prov = FakeProvider(last_day=TODAY)
    svc = MarketDataService(store, prov, today=lambda: TODAY)
    s = dict(DEFAULT_SETTINGS, manual_only=["AMZN|USD"])
    assert "AMZN" not in svc.required_symbols(pf_csv, s) and "AMZN" in svc.required_symbols(pf_csv, DEFAULT_SETTINGS)
    svc.refresh(pf_csv, s)
    assert "AMZN" not in {c[0] for c in prov.calls}


def test_statement_mode_never_touches_the_network(store, pf_csv):
    prov = FakeProvider()
    rep = MarketDataService(store, prov).refresh(pf_csv, dict(DEFAULT_SETTINGS, price_source="statement"))
    assert rep.offline and prov.calls == []
    assert "Offline" in rep.summary()


# ======================================================================================================
# assembling MarketData: priorities, as-of date, benchmark
# ======================================================================================================
@pytest.fixture
def refreshed(store, pf_csv):
    prov = FakeProvider(last_day=TODAY)
    svc = MarketDataService(store, prov, today=lambda: TODAY)
    svc.refresh(pf_csv, DEFAULT_SETTINGS)
    return svc


def test_yahoo_data_beats_statement_data_when_newer(refreshed, pf_csv):
    md = refreshed.build(pf_csv, DEFAULT_SETTINGS)
    assert md.as_of == TODAY and md.mode.startswith("Yahoo Finance")
    assert all(p.source == "Yahoo Finance" and p.day == TODAY for p in md.prices.values())
    assert md.fx_used("HUF") == pytest.approx(md.fx_latest_raw["HUF"].price)
    assert md.benchmark.symbol == "VUAA.L" and md.benchmark.latest.day == TODAY
    assert md.benchmark_price() == pytest.approx(md.benchmark.latest.price)


def test_manual_overrides_beat_everything(refreshed, pf_csv):
    s = dict(DEFAULT_SETTINGS, price_overrides={"AMZN|USD": 300.0, "NOPE|USD": 5.0}, fx_overrides={"HUF": 400.0},
             benchmark_override=123.0)
    md = refreshed.build(pf_csv, s)
    assert md.price("AMZN", "USD") == 300.0 and md.prices["AMZN|USD"].source == "Manual override"
    assert md.raw_prices["AMZN|USD"].source == "Yahoo Finance"
    assert "NOPE|USD" not in md.prices and md.price_overrides == {"AMZN|USD": 300.0}
    assert md.fx_used("HUF") == 400.0 and md.benchmark_price() == 123.0
    assert md.fx.rate("HUF", TODAY) != 400.0                       # history is untouched; only 'latest' is overridden


def test_statement_price_wins_if_the_cache_is_older(store, pf_csv):
    store.upsert_prices("AMZN", [(date(2026, 10, 1), 111.0)], "USD", "Amazon")
    md = MarketDataService(store, today=lambda: TODAY).build(pf_csv, DEFAULT_SETTINGS)
    assert md.prices["AMZN|USD"].price == 248.0 and md.prices["AMZN|USD"].source.startswith("IBKR statement")


def test_cached_prices_in_the_wrong_currency_are_ignored(store, pf_csv):
    store.upsert_prices("AMZN", [(date(2026, 10, 30), 4000.0)], "EUR", "Amazon")
    md = MarketDataService(store, today=lambda: TODAY).build(pf_csv, DEFAULT_SETTINGS)
    assert md.prices["AMZN|USD"].price == 248.0
    assert any("quoted in EUR" in n for n in md.notes)


def test_offline_mode_ignores_the_cache(refreshed, pf_csv):
    md = refreshed.build(pf_csv, dict(DEFAULT_SETTINGS, price_source="statement"))
    assert md.mode.startswith("Statements only") and md.as_of == date(2026, 10, 23)
    assert md.prices["AMZN|USD"].source.startswith("IBKR statement")
    assert md.benchmark.latest is None and any("benchmark" in n.lower() for n in md.notes)


def test_benchmark_quoted_in_eur_is_converted_with_the_fx_series(store, pf_csv):
    prov = FakeProvider(last_day=TODAY)
    svc = MarketDataService(store, prov, today=lambda: TODAY)
    s = dict(DEFAULT_SETTINGS, benchmark_symbol="VUAA.DE")
    svc.refresh(pf_csv, s)
    md = svc.build(pf_csv, s)
    b = md.benchmark
    assert b.currency == "EUR" and len(b.dates) == len(b.usd) == len(b.native)
    eur_per_usd = md.fx.rate("EUR", b.dates[-1])
    assert b.usd[-1] == pytest.approx(b.native[-1] / eur_per_usd)


def test_missing_fx_for_an_active_currency_is_reported_and_blocks_the_workbook(pf_synth, store, offline):
    from ibkr_tracker.models import StockTrade
    from dataclasses import replace
    t = pf_synth.trades[0]
    exotic = replace(pf_synth, trades=[*pf_synth.trades, StockTrade(**{**t.__dict__, "ticker": "ZZZ", "currency": "CHF"})])
    md = MarketDataService(store).build(exotic, offline)
    assert md.missing_fx == ["CHF"]
    with pytest.raises(MissingMarketData, match="CHF"):
        build_workbook(exotic, md, offline)
    offline2 = dict(offline, fx_overrides={"CHF": 0.9})                    # the remedy offered to the user
    md2 = MarketDataService(store).build(exotic, offline2)
    assert md2.missing_fx == [] and md2.fx_used("CHF") == 0.9


def test_placeholder_currency_without_activity_is_not_an_error(pf_csv, store, offline):
    md = MarketDataService(store).build(pf_csv, offline)
    assert "EUR" in pf_csv.currencies() and "EUR" not in pf_csv.active_currencies()
    assert md.missing_fx == []
