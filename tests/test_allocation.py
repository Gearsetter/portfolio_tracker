"""Allocation (donut chart data), classification, sector profiles and their storage / lookup plumbing."""
from __future__ import annotations

import copy

import pytest

from fakes import FakeProvider
from mdfactory import D0, END, daily, fx_eur, market
from synth import make_csv
from ibkr_tracker import allocation as A
from ibkr_tracker import tables as T
from ibkr_tracker.calc import compute_metrics
from ibkr_tracker.config import DEFAULT_SETTINGS
from ibkr_tracker.market import MarketDataService, ProfileResult, ProviderError
from ibkr_tracker.market.providers import YahooProvider, parse_profile_info, parse_search_json
from ibkr_tracker.parsing import parse_statement
from ibkr_tracker.portfolio import merge_statements


def holding(ticker: str, usd: float, *, ccy: str = "USD", cls: str = A.STOCKS, stype: str = "Common stock",
            sector: str = "Technology", cash: bool = False) -> A.Holding:
    key = f"Cash {ccy}" if cash else f"{ticker}|{ccy}"
    return A.Holding(key=key, ticker=f"Cash {ccy}" if cash else ticker, currency=ccy, name=ticker, usd_value=usd,
                     asset_class=A.CASH if cash else cls, stock_type=A.CASH if cash else stype,
                     sector=A.CASH if cash else sector, auto_asset_class=cls, auto_sector=sector, is_cash=cash)


@pytest.fixture(scope="module")
def account():
    """The synthetic account with a closed TSLA position and a dust balance: AAPL (USD), SAP (EUR), cash EUR / USD."""
    data, _ = make_csv(closed_position=True, dust_currency=True)
    pf = merge_statements([parse_statement("synthetic.csv", data)])
    fx = {"EUR": daily(D0, END, fx_eur, True), "GBP": daily(D0, END, lambda d: 0.75, True)}      # 0.45 GBP = 0.60 USD
    return pf, market(pf, fx=fx)


# ---------------------------------------------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------------------------------------------
def test_type_labels_and_fund_detection():
    assert A.type_label("COMMON") == "Common stock" and A.type_label("adr") == "ADR (depositary receipt)"
    assert A.type_label("ETF") == "ETF" and A.type_label("") == A.UNKNOWN_TYPE and A.type_label("Some Thing") == "Some Thing"
    assert [A.is_fund(t) for t in ("ETF", "ETN", "MUTUAL FUND", "CLOSED-END FUND", "COMMON", "ADR", "REIT")] == \
        [True, True, True, True, False, False, False]
    assert A.is_fund("", "ETF") and A.is_fund("", "mutualfund") and not A.is_fund("", "EQUITY") and not A.is_fund("")
    assert not A.is_fund("COMMON", "ETF")                                  # the statement's type wins over Yahoo's


def test_sector_and_asset_class_names_are_matched_to_the_known_ones():
    assert A.canonical_sector("technology") == "Technology" and A.canonical_sector("Information Technology") == "Technology"
    assert A.canonical_sector("health care") == "Healthcare" and A.canonical_sector("Financials") == "Financial Services"
    assert A.canonical_sector("consumer staples") == "Consumer Defensive" and A.canonical_sector("  My  own   group ") == "My own group"
    assert A.canonical_asset_class("equities") == A.STOCKS and A.canonical_asset_class("ETF") == A.FUNDS
    assert A.canonical_asset_class("gold") == "Commodities" and A.canonical_asset_class("Private equity") == "Private equity"
    assert A.clean_name("  ", A.canonical_sector) == ("", "")
    assert A.clean_name("x" * 41, A.canonical_sector)[1].endswith("is longer than 40 characters")
    assert A.clean_name("bad\x07name", A.canonical_sector)[1] == "contains control characters"


# ---------------------------------------------------------------------------------------------------------------
# classification of the holdings of an account
# ---------------------------------------------------------------------------------------------------------------
def test_classify_gives_every_stock_and_cash_balance_a_class(account):
    pf, md = account
    m = compute_metrics(pf, md)
    hs = {h.key: h for h in A.classify(pf, m, {}, {}, hide=False)}
    assert set(hs) == {"AAPL|USD", "SAP|EUR", "Cash EUR", "Cash USD", "Cash GBP"}      # TSLA is closed: not a holding
    aapl, sap = hs["AAPL|USD"], hs["SAP|EUR"]
    assert (aapl.asset_class, aapl.stock_type, aapl.sector, aapl.sector_source) == (
        A.STOCKS, "Common stock", A.UNCLASSIFIED, "not classified yet")
    assert aapl.usd_value == pytest.approx(next(r.usd_value for r in m.positions if r.ticker == "AAPL"))
    assert sap.currency == "EUR" and sap.quantity == pytest.approx(5.0)
    cash = [h for h in hs.values() if h.is_cash]
    assert cash and all(h.asset_class == h.stock_type == h.sector == A.CASH for h in cash)
    assert all(h.sector_source == "cash" for h in cash)                  # not "Yahoo Finance": a balance has no sector source
    assert sum(h.usd_value for h in hs.values()) == pytest.approx(m.stocks_usd + m.cash_usd)


def test_the_view_filter_hides_dust_balances(account):
    pf, md = account
    m = compute_metrics(pf, md)
    shown = {h.key for h in A.classify(pf, m, {}, {}, hide=False)}
    hidden = {h.key for h in A.classify(pf, m, {}, {}, hide=True)}
    assert shown - hidden == {"Cash GBP"} and {"AAPL|USD", "SAP|EUR"} <= hidden        # only the 0.45 GBP dust disappears


def test_sector_precedence_is_your_entry_then_yahoo_then_a_default(account):
    pf, md = account
    m = compute_metrics(pf, md)
    profiles = {"AAPL|USD": {"sector": "Technology", "industry": "Consumer Electronics", "quote_type": "EQUITY"},
                "SAP|EUR": {"sector": "Technology", "industry": "Software - Application", "quote_type": "EQUITY"}}
    hs = {h.key: h for h in A.classify(pf, m, {}, profiles, False)}
    assert hs["AAPL|USD"].sector == "Technology" and hs["AAPL|USD"].sector_source == "Yahoo Finance"
    assert hs["AAPL|USD"].industry == "Consumer Electronics"
    mine = {"classification": {"SAP|EUR": {"sector": "Software", "asset_class": "Other"}}}
    hs = {h.key: h for h in A.classify(pf, m, mine, profiles, False)}
    sap = hs["SAP|EUR"]
    assert (sap.sector, sap.sector_source, sap.asset_class, sap.class_source) == ("Software", "your entry", "Other", "your entry")
    assert (sap.auto_sector, sap.auto_asset_class) == ("Technology", A.STOCKS)         # what it would be without the entry
    # an entry of the bare ticker applies to every listing; the specific key wins over it
    both = {"classification": {"AAPL": {"sector": "Wide"}, "AAPL|USD": {"sector": "Specific"}}}
    assert {h.key: h for h in A.classify(pf, m, both, {}, False)}["AAPL|USD"].sector == "Specific"
    wide = {"classification": {"AAPL": {"sector": "Wide"}}}
    assert {h.key: h for h in A.classify(pf, m, wide, {}, False)}["AAPL|USD"].sector == "Wide"


def test_funds_are_etfs_and_funds_without_a_sector(account):
    pf, md = account
    pf = copy.deepcopy(pf)
    pf.instruments["AAPL"].type = "ETF"
    pf.instruments["SAP"].type = ""                                                   # the statement does not say
    m = compute_metrics(pf, md)
    hs = {h.key: h for h in A.classify(pf, m, {}, {"SAP|EUR": {"sector": "", "quote_type": "ETF"}}, False)}
    assert (hs["AAPL|USD"].asset_class, hs["AAPL|USD"].stock_type, hs["AAPL|USD"].sector) == (A.FUNDS, "ETF", A.FUNDS)
    assert hs["AAPL|USD"].sector_source == "fund (no sector)"
    assert (hs["SAP|EUR"].asset_class, hs["SAP|EUR"].stock_type) == (A.FUNDS, A.UNKNOWN_TYPE)   # Yahoo's quoteType decides
    # a sector Yahoo reports for something the statement calls a fund is shown as reported
    hs = {h.key: h for h in A.classify(pf, m, {}, {"AAPL|USD": {"sector": "Technology"}}, False)}
    assert hs["AAPL|USD"].sector == "Technology" and hs["AAPL|USD"].asset_class == A.FUNDS


# ---------------------------------------------------------------------------------------------------------------
# the weights of one grouping
# ---------------------------------------------------------------------------------------------------------------
HOLDINGS = [holding("AAA", 600.0, sector="Technology"), holding("BBB", 300.0, ccy="EUR", sector="Energy", stype="ADR (depositary receipt)"),
            holding("CCC", 100.0, cls=A.FUNDS, stype="ETF", sector=A.FUNDS), holding("", 100.0, cash=True),
            holding("DDD", 300.0, sector="Technology")]


def test_allocate_sums_weights_orders_by_value_and_groups():
    a = A.allocate(HOLDINGS, "sector")
    assert [(s.label, s.usd_value) for s in a.slices] == [("Technology", 900.0), ("Energy", 300.0), (A.CASH, 100.0), (A.FUNDS, 100.0)]
    assert a.total == 1400.0 and sum(s.weight for s in a.slices) == pytest.approx(1.0)
    assert a.get("Technology").weight == pytest.approx(900 / 1400) and a.get("Technology").members == ["AAA", "DDD"]
    by_class = A.allocate(HOLDINGS, "asset_class")
    assert {s.label: s.usd_value for s in by_class.slices} == {A.STOCKS: 1200.0, A.FUNDS: 100.0, A.CASH: 100.0}
    by_type = A.allocate(HOLDINGS, "stock_type")
    assert {s.label: s.usd_value for s in by_type.slices} == {"Common stock": 900.0, "ADR (depositary receipt)": 300.0, "ETF": 100.0, A.CASH: 100.0}
    by_ccy = A.allocate(HOLDINGS, "currency")
    assert {s.label: s.usd_value for s in by_ccy.slices} == {"USD": 1100.0, "EUR": 300.0}
    by_holding = A.allocate(HOLDINGS, "holding")
    assert [s.label for s in by_holding.slices] == ["AAA", "BBB", "DDD", "CCC", "Cash USD"]       # ties: by name
    with pytest.raises(ValueError):
        A.allocate(HOLDINGS, "colour")


def test_ties_are_ordered_by_name_and_empty_input_is_empty():
    a = A.allocate([holding("B", 50.0, sector="Beta"), holding("A", 50.0, sector="Alpha")], "sector")
    assert [s.label for s in a.slices] == ["Alpha", "Beta"]
    empty = A.allocate([], "sector")
    assert empty.slices == [] and empty.total == 0.0 and A.chart_slices(empty) == []


def test_negative_balances_are_listed_apart_and_do_not_distort_the_weights():
    hs = [holding("AAA", 800.0), holding("", -250.0, cash=True), holding("", 200.0, ccy="EUR", cash=True)]
    a = A.allocate(hs, "asset_class")
    assert a.total == 1000.0 and a.get(A.STOCKS).weight == pytest.approx(0.8) and a.get(A.CASH).weight == pytest.approx(0.2)
    assert a.excluded == [("Cash USD", -250.0)] and sum(s.weight for s in a.slices) == pytest.approx(1.0)


def test_the_unclassified_share():
    hs = [holding("A", 60.0, sector=A.UNCLASSIFIED), holding("B", 40.0, sector="Energy")]
    assert A.allocate(hs, "sector").unclassified_share == pytest.approx(0.6)
    assert A.allocate(hs, "asset_class").unclassified_share == 0.0


def test_small_groups_are_merged_into_other_for_the_chart_only():
    hs = [holding(f"S{i}", float(100 - i * 5), sector=f"Sector {i:02d}") for i in range(14)]
    a = A.allocate(hs, "sector")
    chart = A.chart_slices(a, 10)
    assert len(a.slices) == 14 and len(chart) == 10 and chart[-1].label == "Other (5)"
    assert chart[-1].usd_value == pytest.approx(sum(s.usd_value for s in a.slices[9:]))
    assert sum(s.weight for s in chart) == pytest.approx(1.0) and [s.label for s in chart[:9]] == [s.label for s in a.slices[:9]]
    assert A.chart_slices(a, 14) == a.slices                                          # nothing to merge


def test_concentration_by_hand():
    hs = [holding("A", 60.0), holding("B", 30.0), holding("C", 10.0), holding("", 100.0, cash=True)]
    c = A.concentration(hs)
    assert (c.n_positions, c.largest_ticker) == (3, "A")
    assert c.largest_weight == pytest.approx(0.3) and c.top3_weight == pytest.approx(0.5) and c.top5_weight == pytest.approx(0.5)
    assert c.effective_positions == pytest.approx(1 / (0.6 ** 2 + 0.3 ** 2 + 0.1 ** 2)) and c.cash_weight == pytest.approx(0.5)
    equal = A.concentration([holding(f"S{i}", 10.0) for i in range(10)])
    assert equal.effective_positions == pytest.approx(10.0) and equal.cash_weight == 0.0
    cash_only = A.concentration([holding("", 50.0, cash=True)])
    assert cash_only.n_positions == 0 and cash_only.largest_weight is None and cash_only.cash_weight == 1.0
    assert A.concentration([]).cash_weight == 0.0 and A.concentration([]).effective_positions is None
    cards = T.concentration_cards(c)
    assert [label for label, _, _ in cards][:2] == ["Stock positions", "Largest position (A)"] and cards[0][1] == 3.0


# ---------------------------------------------------------------------------------------------------------------
# the user's entries
# ---------------------------------------------------------------------------------------------------------------
def test_classification_edits_keep_only_what_differs_from_the_automatic_value():
    hs = [holding("AAA", 10.0, sector="Technology"), holding("BBB", 10.0, sector=A.UNCLASSIFIED)]
    ch = A.apply_classification_edits({}, hs, {"AAA|USD": (A.STOCKS, "Technology"), "BBB|USD": (A.STOCKS, "health care")})
    assert ch.classification == {"BBB|USD": {"sector": "Healthcare"}} and ch.changed == ["BBB|USD"] and ch.errors == []
    # an asset class that differs is kept, together with the sector entry that is already there
    ch2 = A.apply_classification_edits({"classification": ch.classification}, hs, {"BBB|USD": ("bonds", "Healthcare")})
    assert ch2.classification == {"BBB|USD": {"asset_class": "Bonds", "sector": "Healthcare"}}
    # clearing a cell (or typing the automatic value) removes the entry
    ch3 = A.apply_classification_edits({"classification": ch2.classification}, hs, {"BBB|USD": ("", "")})
    assert ch3.classification == {} and ch3.changed == ["BBB|USD"]
    # an unchanged table changes nothing
    same = A.apply_classification_edits({"classification": ch.classification}, hs, {"BBB|USD": (A.STOCKS, "Healthcare")})
    assert same.changed == [] and same.classification == ch.classification


def test_classification_edits_report_errors_and_ignore_unknown_rows():
    hs = [holding("AAA", 10.0), holding("", 5.0, cash=True)]
    ch = A.apply_classification_edits({}, hs, {"AAA|USD": (A.STOCKS, "y" * 60), "ZZZ|USD": ("x", "y"), "Cash USD": ("x", "y")})
    assert ch.classification == {} and ch.changed == [] and len(ch.errors) == 1 and ch.errors[0].startswith("AAA:")
    base = {"classification": {"OLD|USD": {"sector": "Energy"}, "bad": "not a dict"}}
    ch = A.apply_classification_edits(base, hs, {"AAA|USD": (A.STOCKS, "Utilities")})
    assert ch.classification == {"OLD|USD": {"sector": "Energy"}, "AAA|USD": {"sector": "Utilities"}}


def test_tables_of_the_allocation():
    a = A.allocate(HOLDINGS, "sector")
    t = T.allocation_table(a)
    assert list(t.columns) == ["Industry sector", "USD Value", "Weight", "Holdings", "Members"]
    assert t["Weight"].sum() == pytest.approx(1.0) and t.loc[0, "Members"] == "AAA, DDD"
    long = T.allocation_long_table([A.allocate(HOLDINGS, by) for by in ("asset_class", "sector")])
    assert set(long["Grouping"]) == {"Asset class", "Industry sector"} and len(long) == 3 + 4
    hold = T.holdings_table(HOLDINGS)
    assert hold["Weight"].sum() == pytest.approx(1.0) and hold.loc[hold["Holding"] == "Cash USD", "Asset class"].iloc[0] == A.CASH
    cls = T.classification_table(HOLDINGS)
    assert list(cls["Ticker"]) == ["AAA", "BBB", "CCC", "DDD"] and "Asset class" in cls and "Sector from" in cls


# ---------------------------------------------------------------------------------------------------------------
# the profiles Yahoo returns
# ---------------------------------------------------------------------------------------------------------------
def test_parse_yfinance_info():
    p = parse_profile_info("AAPL", {"sector": " Technology ", "industry": "Consumer Electronics", "quoteType": "equity",
                                    "longName": "Apple Inc.", "shortName": "Apple", "category": None})
    assert (p.symbol, p.sector, p.industry, p.quote_type, p.name, p.found) == (
        "AAPL", "Technology", "Consumer Electronics", "EQUITY", "Apple Inc.", True)
    etf = parse_profile_info("VUAA.L", {"quoteType": "ETF", "category": "Large Blend", "shortName": "Vanguard S&P 500"})
    assert etf.sector == "" and etf.quote_type == "ETF" and etf.name == "Vanguard S&P 500" and etf.found
    for junk in (None, {}, [], "x", {"sector": 5, "quoteType": None}):
        assert not parse_profile_info("X", junk).found


def test_parse_search_json_picks_the_exact_symbol():
    payload = {"quotes": [{"symbol": "AAPL.MX", "sector": "Other"},
                          {"symbol": "AAPL", "sectorDisp": "Technology", "industryDisp": "Consumer Electronics",
                           "quoteType": "EQUITY", "longname": "Apple Inc.", "shortname": "Apple"}, "junk"]}
    p = parse_search_json("aapl", payload)
    assert (p.sector, p.industry, p.quote_type, p.name) == ("Technology", "Consumer Electronics", "EQUITY", "Apple Inc.")
    with pytest.raises(ProviderError, match="not found"):
        parse_search_json("MSFT", payload)
    with pytest.raises(ProviderError):
        parse_search_json("MSFT", None)
    assert parse_search_json("X", {"quotes": [{"symbol": "X", "sector": "Energy", "quoteType": "EQUITY"}]}).sector == "Energy"


def test_the_yahoo_provider_falls_back_to_the_search_endpoint(monkeypatch):
    prov = YahooProvider()
    calls = []

    def via_yf(symbol):
        calls.append("yf")
        return ProfileResult(symbol)                                                  # Yahoo answered with nothing usable

    def via_search(symbol):
        calls.append("search")
        return ProfileResult(symbol, "Energy", "Oil & Gas", "EQUITY")

    monkeypatch.setattr(prov, "_profile_via_yfinance", via_yf)
    monkeypatch.setattr(prov, "_profile_via_search", via_search)
    assert prov.profile("XOM").sector == "Energy" and calls == ["yf", "search"]
    monkeypatch.setattr(prov, "_profile_via_yfinance", lambda s: ProfileResult(s, "Technology", "", "EQUITY"))
    calls.clear()
    assert prov.profile("MSFT").sector == "Technology" and calls == []
    monkeypatch.setattr(prov, "_profile_via_yfinance", lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(prov, "_profile_via_search", lambda s: (_ for _ in ()).throw(ProviderError("HTTP 403")))
    with pytest.raises(ProviderError) as e:
        prov.profile("KO")
    assert "yfinance: boom" in str(e.value) and "search: HTTP 403" in str(e.value) and str(e.value).startswith("KO:")


# ---------------------------------------------------------------------------------------------------------------
# storage and the service
# ---------------------------------------------------------------------------------------------------------------
def test_profiles_are_stored_and_replaced(store):
    assert store.get_profiles() == {}
    store.save_profile("AAPL|USD", "AAPL", "Technology", "Consumer Electronics", "EQUITY", "Apple Inc.")
    store.save_profile("SAP|EUR", "SAP.DE", "", "", "ETF", "")
    got = store.get_profiles()
    assert got["AAPL|USD"]["sector"] == "Technology" and got["AAPL|USD"]["symbol"] == "AAPL" and got["AAPL|USD"]["fetched_at"]
    assert got["SAP|EUR"]["quote_type"] == "ETF"
    store.save_profile("AAPL|USD", "AAPL", "Healthcare", "", "EQUITY", "Apple")                 # replaced, not duplicated
    assert len(store.get_profiles()) == 2 and store.get_profiles()["AAPL|USD"]["sector"] == "Healthcare"
    assert store.delete_profiles(["SAP|EUR"]) == 1 and list(store.get_profiles()) == ["AAPL|USD"]
    assert store.delete_profiles() == 1 and store.get_profiles() == {}


def lookup_service(store, **kw):
    prov = FakeProvider(**kw)
    return MarketDataService(store, prov), prov


def test_refresh_profiles_looks_up_held_stocks_and_caches_them(store, account):
    pf, _ = account
    svc, prov = lookup_service(store)
    report = svc.refresh_profiles(pf, dict(DEFAULT_SETTINGS))
    assert prov.profile_calls == ["AAPL", "SAP.DE"] and report.ok and report.answered == 2
    assert report.found == {"AAPL|USD": "Technology", "SAP|EUR": "Technology"}        # TSLA is closed: not asked
    assert "2 sector(s) found" in report.summary()
    again = svc.refresh_profiles(pf, dict(DEFAULT_SETTINGS))
    assert prov.profile_calls == ["AAPL", "SAP.DE"] and sorted(again.cached) == ["AAPL|USD", "SAP|EUR"]
    forced = svc.refresh_profiles(pf, dict(DEFAULT_SETTINGS), force=True)
    assert prov.profile_calls[2:] == ["AAPL", "SAP.DE"] and len(forced.found) == 2
    md = svc.build(pf, dict(DEFAULT_SETTINGS))
    assert md.profiles["AAPL|USD"]["industry"] == "Consumer Electronics" and md.profiles["SAP|EUR"]["symbol"] == "SAP.DE"


def test_refresh_profiles_skips_funds_manual_only_and_classified_stocks(store, account):
    pf, _ = account
    pf = copy.deepcopy(pf)
    pf.instruments["AAPL"].type = "ETF"
    svc, prov = lookup_service(store)
    report = svc.refresh_profiles(pf, dict(DEFAULT_SETTINGS))
    assert prov.profile_calls == ["SAP.DE"] and report.skipped == ["AAPL|USD"]
    svc2, prov2 = lookup_service(store)
    s = dict(DEFAULT_SETTINGS, manual_only=["AAPL|USD"], classification={"SAP|EUR": {"sector": "Software"}})
    assert svc2.refresh_profiles(copy.deepcopy(account[0]), s).skipped == ["AAPL|USD", "SAP|EUR"] and prov2.profile_calls == []


def test_a_changed_symbol_invalidates_the_cached_profile(store, account):
    pf, _ = account
    svc, prov = lookup_service(store, profiles={"SAPX": ("Industrials", "", "EQUITY")})
    svc.refresh_profiles(pf, dict(DEFAULT_SETTINGS))
    assert svc.build(pf, dict(DEFAULT_SETTINGS)).profiles["SAP|EUR"]["sector"] == "Technology"
    remapped = dict(DEFAULT_SETTINGS, yahoo_symbols={"SAP|EUR": "SAPX"})
    assert "SAP|EUR" not in svc.build(pf, remapped).profiles                          # looked up for another symbol
    svc.refresh_profiles(pf, remapped)
    assert prov.profile_calls[-1] == "SAPX" and svc.build(pf, remapped).profiles["SAP|EUR"]["sector"] == "Industrials"


def test_refresh_profiles_reports_failures(store):
    data, _ = make_csv()
    pf = merge_statements([parse_statement("synthetic.csv", data)])
    svc, prov = lookup_service(store, profile_fail=("SAP.DE",))
    report = svc.refresh_profiles(pf, dict(DEFAULT_SETTINGS))
    assert report.found == {"AAPL|USD": "Technology"} and list(report.errors) == ["SAP|EUR"] and not report.ok
    assert "simulated outage" in report.errors["SAP|EUR"] and "1 failed" in report.summary()
    assert set(store.get_profiles()) == {"AAPL|USD"}                                   # a failure caches nothing
    prov.profile_fail.clear()
    assert svc.refresh_profiles(pf, dict(DEFAULT_SETTINGS)).found == {"SAP|EUR": "Technology"}   # AAPL is already known


def test_refresh_profiles_gives_up_when_nothing_is_answered(store, pf_csv):
    svc, prov = lookup_service(store)
    prov.profiles = {}                                                                 # every lookup fails: "not found"
    report = svc.refresh_profiles(pf_csv, dict(DEFAULT_SETTINGS))
    assert len(prov.profile_calls) == svc.GIVE_UP_AFTER == 3 and report.aborted and report.answered == 0
    assert len(report.errors) == 3 and "could not be reached" in report.messages[0] and "3 stock(s)" in report.messages[0]
    seen = []
    svc.refresh_profiles(pf_csv, dict(DEFAULT_SETTINGS), progress=lambda i, n, sym: seen.append((i, n, sym)))
    assert seen[0][:2] == (0, 6) and seen[-1] == (6, 6, "")                           # progress: 6 stocks, then done


def test_one_success_keeps_the_lookup_going(store, pf_csv):
    svc, prov = lookup_service(store)
    first = svc.symbol_for(pf_csv, dict(DEFAULT_SETTINGS), *pf_csv.positions_keys()[0])
    prov.profiles = {first: ("Energy", "Oil", "EQUITY")}                              # only the first stock is known
    report = svc.refresh_profiles(pf_csv, dict(DEFAULT_SETTINGS))
    assert report.answered == 1 and len(report.found) == 1 and len(report.errors) == 5 and not report.aborted


def test_refresh_profiles_offline_or_without_a_profile_capability(store, account):
    pf, _ = account
    svc, prov = lookup_service(store)
    off = svc.refresh_profiles(pf, dict(DEFAULT_SETTINGS, price_source="statement"))
    assert off.offline and not off.ok and "statements only" in off.summary() and prov.profile_calls == []

    class PricesOnly:
        def history(self, *a, **k):
            raise AssertionError("not used")

    bare = MarketDataService(store, PricesOnly())
    none = bare.refresh_profiles(pf, dict(DEFAULT_SETTINGS))
    assert none.unsupported and "cannot look up sectors" in none.summary()


def test_profiles_never_change_a_valuation(store, account):
    pf, md = account
    svc, _ = lookup_service(store)
    before = compute_metrics(pf, svc.build(pf, dict(DEFAULT_SETTINGS))).usd_total
    svc.refresh_profiles(pf, dict(DEFAULT_SETTINGS))
    assert compute_metrics(pf, svc.build(pf, dict(DEFAULT_SETTINGS))).usd_total == before
