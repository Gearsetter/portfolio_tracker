"""Manual ticker mapping (alias override): IBKR ticker -> the symbol the data provider knows it under.

The aliases live in the settings of the data folder - they are persistent across sessions and used by every download,
every workbook and every account - and can be edited, tested, exported and imported."""
from __future__ import annotations

from datetime import date

import openpyxl
import pytest

from conftest import TODAY
from fakes import FakeProvider
from mdfactory import daily
from synth import ACCOUNT, USD_ACCOUNT, make_csv, make_usd_csv
from ibkr_tracker.config import DEFAULT_SETTINGS
from ibkr_tracker.market.mapping import (HEADERS, apply_edits, check_symbol, clean_symbol, mapping_rows,
                                         mapping_to_csv, merge_import, parse_mapping_csv, validate_symbol)
from ibkr_tracker.market.providers import SeriesResult
from ibkr_tracker.market.symbols import yahoo_symbol
from ibkr_tracker.parsing import parse_statement
from ibkr_tracker.portfolio import merge_statements
from ibkr_tracker.storage import Store
from ibkr_tracker.tracker import Tracker

D = date
BLANK_MSG = "a symbol must not contain blanks (Yahoo writes 'BRK-B' for IBKR's 'BRK B')"


# ======================================================================================================
# symbols
# ======================================================================================================
@pytest.mark.parametrize("symbol", ["AAPL", "BRK-B", "NOVO-B.CO", "^GSPC", "EURUSD=X", "0700.HK", "7203.T", "SAP.DE",
                                    "A_B", "S&P", "A" * 32])
def test_valid_symbols(symbol):
    assert validate_symbol(symbol) is None


@pytest.mark.parametrize("symbol, why", [("", "empty"), ("   ", "empty"), ("BRK B", "blanks"), ("AA\tPL", "blanks"),
                                         ("AAPL!", "only letters"), (".AAPL", "only letters"), ("-X", "only letters"),
                                         ("A" * 33, "only letters"), ("ÄPL", "only letters")])
def test_invalid_symbols(symbol, why):
    problem = validate_symbol(symbol)
    assert problem and why in problem


def test_what_the_user_types_is_normalised():
    assert clean_symbol("  brk-b ") == "BRK-B" and clean_symbol(None) == "" and clean_symbol("sap.de") == "SAP.DE"


def test_an_alias_wins_over_the_automatic_rule_and_the_specific_key_over_the_general_one():
    assert yahoo_symbol("SAP", "IBIS", "EUR") == "SAP.DE" and yahoo_symbol("BRK B", "NYSE", "USD") == "BRK-B"
    assert yahoo_symbol("SAP", "IBIS", "EUR", {"SAP|EUR": "SAP.F"}) == "SAP.F"
    assert yahoo_symbol("SAP", "IBIS", "EUR", {"SAP": "SAP.MU"}) == "SAP.MU"                  # every currency
    assert yahoo_symbol("SAP", "IBIS", "EUR", {"SAP": "SAP.MU", "SAP|EUR": "SAP.F"}) == "SAP.F"
    assert yahoo_symbol("SAP", "IBIS", "USD", {"SAP|EUR": "SAP.F"}) == "SAP.DE"                # another currency: untouched
    assert yahoo_symbol("SAP", "IBIS", "EUR", {"SAP|EUR": ""}) == "SAP.DE"                      # an empty alias is none


# ======================================================================================================
# the table behind the editor
# ======================================================================================================
def by_ticker(rows):
    return {r.ticker: r for r in rows}


def test_rows_show_the_automatic_symbol_and_the_state_of_the_download(store, service, pf_synth):
    rows = by_ticker(mapping_rows(store, service, pf_synth, DEFAULT_SETTINGS))
    assert sorted(rows) == ["AAPL", "SAP"]
    sap = rows["SAP"]
    assert (sap.key, sap.currency, sap.exchange, sap.name) == ("SAP|EUR", "EUR", "IBIS", "SAP SE")
    assert (sap.auto_symbol, sap.alias, sap.symbol, sap.source) == ("SAP.DE", "", "SAP.DE", "automatic")
    assert sap.held and not sap.manual_only and not sap.problem and sap.points == 0 and sap.last_price is None
    assert sap.status == "not downloaded yet - press Refresh Prices"


def test_rows_of_a_closed_position_are_not_held(store, service):
    data, _ = make_csv(closed_position=True)
    pf = merge_statements([parse_statement("c.csv", data)])
    rows = by_ticker(mapping_rows(store, service, pf, DEFAULT_SETTINGS))
    assert not rows["TSLA"].held and rows["AAPL"].held and rows["SAP"].held


def test_rows_with_an_alias(store, service, pf_synth):
    settings = dict(DEFAULT_SETTINGS, yahoo_symbols={"SAP|EUR": "SAP.F", "AAPL": "AAPL.MX"})
    rows = by_ticker(mapping_rows(store, service, pf_synth, settings))
    assert (rows["SAP"].alias, rows["SAP"].symbol, rows["SAP"].source) == ("SAP.F", "SAP.F", "your alias")
    assert (rows["AAPL"].alias, rows["AAPL"].auto_symbol, rows["AAPL"].symbol) == ("AAPL.MX", "AAPL", "AAPL.MX")


def test_row_status_follows_the_cache_the_errors_and_the_manual_prices(store, service, pf_synth):
    rows = lambda s=DEFAULT_SETTINGS: by_ticker(mapping_rows(store, service, pf_synth, s))          # noqa: E731
    store.upsert_prices("AAPL", daily(D(2025, 3, 3), D(2025, 3, 7), lambda d: 1.0, True), "USD", "Apple")
    r = rows()["AAPL"]
    assert r.status == "ok - 5 prices, newest 2025-03-07" and r.points == 5 and r.last_price == D(2025, 3, 7)
    store.save_manual_prices("AAPL|USD", [(D(2025, 3, 10), 2.0), (D(2025, 3, 11), 2.0)])
    assert rows()["AAPL"].status == "ok - 5 prices, newest 2025-03-07 + 2 manual"
    assert rows()["AAPL"].manual_points == 2

    store.set_price_error("SAP.DE", "no data found")                                                # failed, nothing cached
    r = rows()["SAP"]
    assert r.status == "last download failed: no data found" and r.problem
    store.save_manual_prices("SAP|EUR", [(D(2025, 3, 10), 120.0)])                                   # ... but prices typed in
    assert not rows()["SAP"].problem
    store.upsert_prices("AAPL", [(D(2025, 3, 7), 1.0)], None, None)
    store.set_price_error("AAPL", "timeout")                                                         # failed, cache present
    r = rows()["AAPL"]
    assert r.status.startswith("last download failed (timeout); cached prices up to 2025-03-07 are used")
    assert not r.problem


def test_a_symbol_quoted_in_another_currency_is_flagged(store, service, pf_synth):
    store.upsert_prices("SAP.DE", [(D(2025, 3, 3), 120.0)], "USD", "wrong listing")
    r = by_ticker(mapping_rows(store, service, pf_synth, DEFAULT_SETTINGS))["SAP"]
    assert r.problem and r.status == "quoted in USD, IBKR trades it in EUR - wrong symbol? cached prices are ignored"
    md = service.build(pf_synth, DEFAULT_SETTINGS)
    assert any("SAP: Yahoo symbol SAP.DE is quoted in USD but IBKR trades it in EUR" in n for n in md.notes)


def test_manual_only_rows(store, service, pf_synth):
    settings = dict(DEFAULT_SETTINGS, manual_only=["SAP|EUR"])
    r = by_ticker(mapping_rows(store, service, pf_synth, settings))["SAP"]
    assert r.manual_only and r.source == "manual prices only" and r.problem
    assert r.status == "manual prices only - none entered yet (Manual price history)"
    store.save_manual_prices("SAP|EUR", [(D(2025, 3, 3), 120.0), (D(2025, 3, 4), 121.0)])
    r = by_ticker(mapping_rows(store, service, pf_synth, settings))["SAP"]
    assert not r.problem and r.status == "manual prices only - 2 price(s) entered"


# ======================================================================================================
# edits
# ======================================================================================================
@pytest.fixture
def rows(store, service, pf_synth):
    def make(settings=None):
        return mapping_rows(store, service, pf_synth, settings or DEFAULT_SETTINGS)
    return make


def test_a_typed_alias_is_normalised_and_saved(rows):
    ch = apply_edits(DEFAULT_SETTINGS, rows(), {"SAP|EUR": (" sap.f ", False)})
    assert ch.aliases == {"SAP|EUR": "SAP.F"} and ch.manual_only == [] and ch.changed == ["SAP|EUR"] and ch.errors == []


def test_an_alias_equal_to_the_automatic_symbol_is_no_override(rows):
    ch = apply_edits(DEFAULT_SETTINGS, rows(), {"SAP|EUR": ("sap.de", False)})
    assert ch.aliases == {} and ch.changed == []
    saved = dict(DEFAULT_SETTINGS, yahoo_symbols={"SAP|EUR": "SAP.F"})
    ch = apply_edits(saved, rows(saved), {"SAP|EUR": ("SAP.DE", False)})                  # typing the default removes it
    assert ch.aliases == {} and ch.changed == ["SAP|EUR"]


def test_rows_that_were_not_edited_are_not_touched(rows):
    saved = dict(DEFAULT_SETTINGS, yahoo_symbols={"SAP|EUR": "SAP.F"}, manual_only=["AAPL|USD"])
    ch = apply_edits(saved, rows(saved), {"SAP|EUR": ("SAP.F", False), "AAPL|USD": ("", True)})
    assert ch.changed == [] and ch.aliases == {"SAP|EUR": "SAP.F"} and ch.manual_only == ["AAPL|USD"]


def test_clearing_an_alias_also_clears_the_ticker_wide_entry_it_came_from(rows):
    saved = dict(DEFAULT_SETTINGS, yahoo_symbols={"SAP": "SAP.MU", "OTHER|USD": "OTHER"})
    assert by_ticker(rows(saved))["SAP"].alias == "SAP.MU"
    ch = apply_edits(saved, rows(saved), {"SAP|EUR": ("", False)})
    assert ch.aliases == {"OTHER|USD": "OTHER"} and ch.changed == ["SAP|EUR"]


def test_stocks_that_are_not_shown_keep_their_entries(rows):
    """Another account's stock (KO) is not in this account's table: saving must not drop its alias."""
    saved = dict(DEFAULT_SETTINGS, yahoo_symbols={"KO|USD": "KO"}, manual_only=["MSFT|USD"])
    ch = apply_edits(saved, rows(saved), {"SAP|EUR": ("SAP.F", False), "KO|USD": ("", False), "NOPE|USD": ("X", False)})
    assert ch.aliases == {"KO|USD": "KO", "SAP|EUR": "SAP.F"} and ch.manual_only == ["MSFT|USD"]
    assert ch.changed == ["SAP|EUR"]


def test_an_invalid_alias_is_rejected_and_the_other_edits_still_apply(rows):
    ch = apply_edits(DEFAULT_SETTINGS, rows(), {"SAP|EUR": ("SAP DE", False), "AAPL|USD": ("aapl.us", False)})
    assert ch.errors == [f"SAP: 'SAP DE' rejected - {BLANK_MSG}."]
    assert ch.aliases == {"AAPL|USD": "AAPL.US"} and ch.changed == ["AAPL|USD"]


def test_the_manual_only_switch(rows):
    ch = apply_edits(DEFAULT_SETTINGS, rows(), {"SAP|EUR": ("", True)})
    assert ch.manual_only == ["SAP|EUR"] and ch.aliases == {} and ch.changed == ["SAP|EUR"]
    saved = dict(DEFAULT_SETTINGS, manual_only=["SAP|EUR"])
    ch = apply_edits(saved, rows(saved), {"SAP|EUR": ("", False)})
    assert ch.manual_only == [] and ch.changed == ["SAP|EUR"]


# ======================================================================================================
# the mapping file
# ======================================================================================================
def test_the_mapping_file_round_trips():
    settings = {"yahoo_symbols": {"SAP|EUR": "SAP.F", "NOVOB": "NOVO-B.CO"}, "manual_only": ["KO|USD"]}
    text = mapping_to_csv(settings)
    assert text.splitlines() == ["Ticker,Currency,Symbol,Manual only", "KO,USD,,yes", "NOVOB,,NOVO-B.CO,",
                                 "SAP,EUR,SAP.F,"]
    assert tuple(text.splitlines()[0].split(",")) == HEADERS
    entries, problems = parse_mapping_csv(text)
    assert problems == [] and entries == {"KO|USD": ("", True), "NOVOB": ("NOVO-B.CO", False),
                                          "SAP|EUR": ("SAP.F", False)}
    assert mapping_to_csv({}) == "Ticker,Currency,Symbol,Manual only\n"


def test_a_mapping_file_may_use_semicolons_tabs_bom_and_loose_spelling():
    for sep in (",", ";", "\t"):
        text = "﻿" + sep.join(["Ticker", "Currency", "Symbol", "Manual only"]) + "\r\n" + \
               sep.join(["SAP", "eur", "sap.f", ""]) + "\r\n\r\n" + sep.join(["KO", "USD", "", "X"]) + "\r\n"
        assert parse_mapping_csv(text) == ({"SAP|EUR": ("SAP.F", False), "KO|USD": ("", True)}, []), repr(sep)
    for word in ("yes", "Y", "TRUE", "1", "x"):
        assert parse_mapping_csv(f"Ticker,Symbol,Manual only\nKO,,{word}\n")[0] == {"KO": ("", True)}
    assert parse_mapping_csv("Symbol,Ticker\nSAP.F,SAP\n")[0] == {"SAP": ("SAP.F", False)}     # any column order


def test_problems_in_a_mapping_file_are_listed_by_line():
    text = ("Ticker,Currency,Symbol,Manual only\nSAP,EUR,sap.f,\nKO,USD,,yes\nBAD,USD,BAD SYM,\n,USD,X,\nZZZ,USD,,\n")
    entries, problems = parse_mapping_csv(text)
    assert entries == {"SAP|EUR": ("SAP.F", False), "KO|USD": ("", True)}
    assert problems == [f"line 4: 'BAD SYM' rejected - {BLANK_MSG}", "line 5: no ticker"]     # ZZZ: nothing to apply


@pytest.mark.parametrize("text, problem", [("", "The file is empty."), ("  \n\n", "The file is empty."),
                                           ("SAP,EUR,SAP.F\n", "The first line must name the columns")])
def test_a_file_that_is_no_mapping(text, problem):
    entries, problems = parse_mapping_csv(text)
    assert entries == {} and len(problems) == 1 and problems[0].startswith(problem)


def test_importing_merges_on_top_of_the_saved_mapping():
    saved = {"yahoo_symbols": {"SAP|EUR": "SAP.DE", "AAPL|USD": "AAPL"}, "manual_only": ["KO|USD"]}
    ch = merge_import(saved, {"SAP|EUR": ("SAP.F", False), "KO|USD": ("", False), "AAPL|USD": ("AAPL", False),
                              "NEW|USD": ("", True)})
    assert ch.aliases == {"SAP|EUR": "SAP.F", "AAPL|USD": "AAPL"} and ch.manual_only == ["NEW|USD"]
    assert ch.changed == ["SAP|EUR", "KO|USD", "NEW|USD"]                                        # AAPL: nothing changed


# ======================================================================================================
# testing a symbol
# ======================================================================================================
class _Stub:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, []

    def history(self, symbol, start, end, include_intraday=False):
        self.calls.append((symbol, start, end))
        if self.error:
            raise self.error
        return self.result


def test_a_symbol_that_exists():
    fake = FakeProvider(levels={"X.Y": 55.0}, last_day=TODAY)
    res = check_symbol(fake, " x.y ", "USD", today=TODAY)
    last = fake.history("X.Y", TODAY.replace(day=16), TODAY).rows[-1]
    assert res.ok and res.symbol == "X.Y" and res.currency == "USD" and res.currency_ok and res.name == "Fake X.Y"
    assert (res.last_day, res.last_price) == (D(2026, 10, 30), last[1])
    assert res.message == f"X.Y: last close {last[1]:,.4f} USD on 2026-10-30 - Fake X.Y"
    assert fake.calls[0][:3] == ("X.Y", D(2026, 10, 16), TODAY)                                  # the last two weeks


def test_a_symbol_quoted_in_another_currency_is_accepted_with_a_warning():
    res = check_symbol(FakeProvider(last_day=TODAY), "VUAA.DE", "USD", today=TODAY)
    assert res.ok and not res.currency_ok and res.currency == "EUR"
    assert res.message.endswith("Warning: IBKR trades this stock in USD, this symbol is quoted in EUR.")
    assert check_symbol(FakeProvider(last_day=TODAY), "VUAA.DE", "", today=TODAY).currency_ok       # nothing to compare


def test_a_symbol_the_provider_does_not_know():
    res = check_symbol(FakeProvider(fail=("BAD.X",)), "bad.x", "USD", today=TODAY)
    assert not res.ok and res.message == "Yahoo Finance has no data for BAD.X: BAD.X: simulated outage"
    res = check_symbol(FakeProvider(last_day=D(2020, 1, 1)), "OLD", today=TODAY)                  # nothing in two weeks
    assert not res.ok and "no rows" in res.message
    stub = _Stub(SeriesResult(symbol="E", rows=[], currency="USD"))
    assert check_symbol(stub, "E", today=TODAY).message == "E: no prices returned."


def test_network_trouble_is_reported_not_raised():
    res = check_symbol(_Stub(error=ConnectionError("proxy refused")), "AAPL", today=TODAY)
    assert not res.ok and res.message == "Could not reach Yahoo Finance: ConnectionError: proxy refused"


def test_an_invalid_symbol_is_not_even_sent():
    stub = _Stub(SeriesResult(symbol="X", rows=[(TODAY, 1.0)], currency="USD"))
    res = check_symbol(stub, "BRK B", "USD", today=TODAY)
    assert not res.ok and res.message == f"Not a valid symbol: {BLANK_MSG}." and stub.calls == []


# ======================================================================================================
# persistent, and used by the whole pipeline
# ======================================================================================================
@pytest.fixture
def tr(home):
    fake = FakeProvider(fail=("SAP.DE",), last_day=TODAY,                  # Yahoo does not know SAP under SAP.DE
                        currencies={"SAP.F": "EUR", "SAP.G": "EUR"})
    t = Tracker(Store(home / "t.db"), fake, today=lambda: TODAY)
    t.update_settings(export_excel=True, recalc_with_libreoffice=False)
    assert t.import_statement("synthetic.csv", make_csv()[0]).ok
    return t, fake, t.portfolio(ACCOUNT)


def test_a_failing_ticker_is_repaired_with_an_alias(tr, home):
    t, fake, p = tr
    out = t.refresh_prices(ACCOUNT)
    assert "SAP.DE" in out.report.errors and "AAPL" in out.report.fetched
    rows = by_ticker(t.mapping_rows(p))
    assert rows["SAP"].problem and rows["SAP"].status == "last download failed: SAP.DE: simulated outage"
    assert rows["AAPL"].status.startswith("ok - ") and not rows["AAPL"].problem
    before = t.snapshot(ACCOUNT)
    assert before.md.price("SAP", "EUR") == 130.0                                       # only the statement close

    fake.calls.clear()
    res = t.save_mapping(p, {"SAP|EUR": ("sap.f", False)})
    assert res.changed == ["SAP|EUR"] and res.errors == [] and list(res.report.fetched) == ["SAP.F"]
    assert [c[0] for c in fake.calls] == ["SAP.F"]                                       # only the changed symbol
    assert res.messages[0] == "Mapping saved (1 stock(s) changed)." and res.messages[1].startswith("Updated 1 symbol(s)")
    assert t.settings()["yahoo_symbols"] == {"SAP|EUR": "SAP.F"}

    row = by_ticker(t.mapping_rows(p))["SAP"]
    assert (row.alias, row.symbol, row.source, row.problem) == ("SAP.F", "SAP.F", "your alias", False)
    assert row.status.startswith("ok - ")
    snap = t.snapshot(ACCOUNT)
    assert snap.md.yahoo_symbols["SAP|EUR"] == "SAP.F"
    pt = snap.md.raw_prices["SAP|EUR"]
    assert (pt.source, pt.symbol) == ("Yahoo Finance", "SAP.F") and pt.day == D(2026, 10, 30)
    assert snap.md.price("SAP", "EUR") != 130.0
    assert not any(n.startswith("SAP:") for n in snap.md.notes)
    h = snap.history()
    assert h.coverage_for("SAP", "EUR").points > 200 and h.matches(snap.metrics) == []

    fake.calls.clear()                                                                   # the next refresh uses the alias
    t.refresh_prices(ACCOUNT)
    symbols = [c[0] for c in fake.calls]
    assert "SAP.F" in symbols and "SAP.DE" not in symbols and "AAPL" in symbols

    again = Tracker(Store(home / "t.db"))                                                # a new session: still there
    assert again.settings()["yahoo_symbols"] == {"SAP|EUR": "SAP.F"}
    assert by_ticker(again.mapping_rows(again.portfolio(ACCOUNT)))["SAP"].symbol == "SAP.F"


def test_the_workbook_carries_the_alias_symbol(tr):
    t, fake, p = tr
    t.save_mapping(p, {"SAP|EUR": ("SAP.F", False)})
    out = t.export(ACCOUNT)
    assert out.ok, out.errors
    ws = openpyxl.load_workbook(t.workbook_path(p))["Prices"]
    r = next(r for r in range(1, 60) if ws[f"B{r}"].value == "SAP")
    assert (ws[f"D{r}"].value, ws[f"G{r}"].value) == ("SAP.F", "Yahoo Finance")
    r = next(r for r in range(1, 60) if ws[f"B{r}"].value == "AAPL")
    assert ws[f"D{r}"].value == "AAPL"


def test_saving_without_a_download_or_in_statement_mode_downloads_nothing(tr):
    t, fake, p = tr
    res = t.save_mapping(p, {"SAP|EUR": ("SAP.F", False)}, download=False)
    assert res.changed == ["SAP|EUR"] and res.report is None and fake.calls == []
    t.update_settings(price_source="statement")
    res = t.save_mapping(p, {"SAP|EUR": ("SAP.G", False)})
    assert res.changed == ["SAP|EUR"] and res.report is None and fake.calls == []
    assert t.settings()["yahoo_symbols"] == {"SAP|EUR": "SAP.G"}


def test_saving_an_unchanged_mapping_does_nothing(tr):
    t, fake, p = tr
    res = t.save_mapping(p, {"SAP|EUR": ("", False), "AAPL|USD": ("aapl", False)})
    assert res.changed == [] and res.report is None and res.messages == [] and fake.calls == []
    assert t.settings()["yahoo_symbols"] == {}


def test_an_invalid_alias_is_reported_and_nothing_else_is_lost(tr):
    t, fake, p = tr
    res = t.save_mapping(p, {"SAP|EUR": ("SAP DE", False), "AAPL|USD": ("aapl.us", False)})
    assert res.errors == [f"SAP: 'SAP DE' rejected - {BLANK_MSG}."] and res.changed == ["AAPL|USD"]
    assert t.settings()["yahoo_symbols"] == {"AAPL|USD": "AAPL.US"} and [c[0] for c in fake.calls] == ["AAPL.US"]


def test_a_stock_can_be_switched_to_manual_prices_only(tr):
    t, fake, p = tr
    res = t.save_mapping(p, {"SAP|EUR": ("", True)})
    assert res.changed == ["SAP|EUR"] and res.report is None                             # nothing to download for it
    assert t.settings()["manual_only"] == ["SAP|EUR"]
    assert "SAP.DE" not in t.service.required_symbols(p, t.settings())
    row = by_ticker(t.mapping_rows(p))["SAP"]
    assert row.problem and row.status == "manual prices only - none entered yet (Manual price history)"
    t.refresh_prices(ACCOUNT)
    assert "SAP.DE" not in [c[0] for c in fake.calls]                                    # never asked for
    t.save_manual_prices("SAP|EUR", [(D(2026, 10, 29), 140.0)])
    snap = t.snapshot(ACCOUNT)
    assert snap.md.price("SAP", "EUR") == 140.0 and "SAP.DE" not in snap.md.notes
    assert by_ticker(t.mapping_rows(p))["SAP"].status == "manual prices only - 1 price(s) entered"
    t.save_mapping(p, {"SAP|EUR": ("SAP.F", False)})                                      # and back to downloading
    assert t.settings()["manual_only"] == [] and [c[0] for c in fake.calls][-1] == "SAP.F"


def test_the_mapping_of_every_account_is_kept_when_one_account_is_edited(home):
    fake = FakeProvider(last_day=TODAY)
    t = Tracker(Store(home / "t.db"), fake, today=lambda: TODAY)
    t.update_settings(export_excel=False, recalc_with_libreoffice=False, yahoo_symbols={"KO|USD": "KO.MX"},
                      manual_only=["MSFT|USD"])
    assert t.import_statement("e.csv", make_csv()[0]).ok and t.import_statement("u.csv", make_usd_csv()[0]).ok
    eur, usd, cons = t.portfolio(ACCOUNT), t.portfolio(USD_ACCOUNT), t.consolidated_portfolio()
    assert sorted(r.ticker for r in t.mapping_rows(eur)) == ["AAPL", "SAP"]
    assert sorted(r.ticker for r in t.mapping_rows(usd)) == ["KO", "MSFT"]
    assert sorted(r.ticker for r in t.mapping_rows(cons)) == ["AAPL", "KO", "MSFT", "SAP"]
    t.save_mapping(eur, {"SAP|EUR": ("SAP.F", False)}, download=False)
    assert t.settings()["yahoo_symbols"] == {"KO|USD": "KO.MX", "SAP|EUR": "SAP.F"}
    assert t.settings()["manual_only"] == ["MSFT|USD"]
    rows = by_ticker(t.mapping_rows(cons))                                                # all views read the same mapping
    assert rows["KO"].symbol == "KO.MX" and rows["SAP"].symbol == "SAP.F" and rows["MSFT"].manual_only


def test_importing_a_mapping_file(tr):
    t, fake, p = tr
    text = "Ticker,Currency,Symbol,Manual only\nSAP,EUR,sap.f,\nKO,USD,KO.MX,\nBAD,USD,BAD SYM,\n"
    res = t.import_mapping(p, text)
    assert res.changed == ["SAP|EUR", "KO|USD"] and res.errors == [f"line 4: 'BAD SYM' rejected - {BLANK_MSG}"]
    assert t.settings()["yahoo_symbols"] == {"SAP|EUR": "SAP.F", "KO|USD": "KO.MX"}
    assert [c[0] for c in fake.calls] == ["SAP.F"]                                       # KO is not in this portfolio
    assert t.mapping_csv() == "Ticker,Currency,Symbol,Manual only\nKO,USD,KO.MX,\nSAP,EUR,SAP.F,\n"
    res = t.import_mapping(p, t.mapping_csv(), download=False)                            # importing it again: no change
    assert res.changed == [] and res.messages == []


def test_a_ticker_wide_alias_from_a_file_applies_to_the_stock_and_is_downloaded(tr):
    t, fake, p = tr
    res = t.import_mapping(p, "Ticker,Symbol\nSAP,SAP.F\n")
    assert res.changed == ["SAP"] and [c[0] for c in fake.calls] == ["SAP.F"]
    assert by_ticker(t.mapping_rows(p))["SAP"].symbol == "SAP.F"
    assert t.snapshot(ACCOUNT).md.yahoo_symbols["SAP|EUR"] == "SAP.F"


def test_the_symbol_test_button(tr):
    t, fake, p = tr
    ok = t.check_symbol("sap.f", "EUR")
    assert ok.ok and ok.symbol == "SAP.F" and ok.last_day == D(2026, 10, 30) and ok.currency == "EUR" and ok.currency_ok
    wrong = t.check_symbol("SAP.X", "EUR")                                                    # the fake quotes it in USD
    assert wrong.ok and not wrong.currency_ok and "Warning: IBKR trades this stock in EUR" in wrong.message
    assert not t.check_symbol("SAP.DE", "EUR").ok and not t.check_symbol("SAP DE").ok
    assert t.settings()["yahoo_symbols"] == {}                                                # testing saves nothing


def test_an_alias_for_the_wrong_listing_is_caught_and_its_prices_are_not_used(tr):
    t, fake, p = tr
    t.save_mapping(p, {"SAP|EUR": ("SAP.X", False)})                                          # quoted in USD, SAP is EUR
    row = by_ticker(t.mapping_rows(p))["SAP"]
    assert row.problem and row.status == "quoted in USD, IBKR trades it in EUR - wrong symbol? cached prices are ignored"
    snap = t.snapshot(ACCOUNT)
    assert any("SAP: Yahoo symbol SAP.X is quoted in USD but IBKR trades it in EUR" in n for n in snap.md.notes)
    assert snap.md.price("SAP", "EUR") == 130.0                                               # the statement close instead


# ======================================================================================================
# the table of the editor
# ======================================================================================================
def test_the_table_of_the_editor(store, service):
    from ibkr_tracker import tables as T
    data, _ = make_csv(closed_position=True)
    pf = merge_statements([parse_statement("c.csv", data)])
    settings = dict(DEFAULT_SETTINGS, yahoo_symbols={"SAP|EUR": "SAP.F"}, manual_only=["TSLA|USD"])
    store.save_manual_prices("SAP|EUR", [(D(2025, 3, 3), 120.0), (D(2025, 3, 4), 121.0)])
    df = T.mapping_table(mapping_rows(store, service, pf, settings))
    assert list(df.columns) == ["Ticker", "Currency", "Held now", "Automatic symbol", "Your symbol (alias)",
                                "Manual prices only", "Symbol used", "Status", "Name", "IBKR exchange"]
    assert df["Ticker"].tolist() == ["AAPL", "SAP", "TSLA"] and df["Currency"].tolist() == ["USD", "EUR", "USD"]
    assert df["Automatic symbol"].tolist() == ["AAPL", "SAP.DE", "TSLA"]
    assert df["Your symbol (alias)"].tolist() == ["", "SAP.F", ""]                      # what the editor lets you change
    assert df["Manual prices only"].tolist() == [False, False, True] and df["Held now"].tolist() == [True, True, False]
    assert df["Symbol used"].tolist() == ["AAPL", "SAP.F", "-"]
    assert df["Status"].tolist() == ["not downloaded yet - press Refresh Prices", "no download yet; 2 manual price(s)",
                                     "manual prices only - none entered yet (Manual price history)"]
    assert df["Held now"].dtype == bool and df["Manual prices only"].dtype == bool
