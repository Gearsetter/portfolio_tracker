"""Manual historical price fallback: the ``Date, Price`` reader, its storage, and the way the prices flow into the
valuation (current price, daily series, benchmark, Excel) next to the downloaded ones."""
from __future__ import annotations

from datetime import date, timedelta

import openpyxl
import pytest

from conftest import TODAY
from fakes import FakeProvider
from mdfactory import daily
from synth import ACCOUNT, make_csv
from ibkr_tracker.calc import compute_metrics
from ibkr_tracker.config import DEFAULT_SETTINGS
from ibkr_tracker.excel import layout as L
from ibkr_tracker.history import build_history
from ibkr_tracker.market.data import SRC_MANUAL, SRC_STATEMENT, SRC_YAHOO, PriceSeries, merge_series
from ibkr_tracker.parsing import parse_statement
from ibkr_tracker.portfolio import merge_statements
from ibkr_tracker.market.manual import (BENCHMARK_KEY, PriceParseError, decode_bytes, detect_decimal, detect_delimiter,
                                        make_key, parse_day, parse_key, parse_price, parse_price_file, parse_prices,
                                        prices_to_text)
from ibkr_tracker.storage import Store
from ibkr_tracker.tracker import Tracker

FAR = date(2100, 1, 1)                      # "no future limit" for tests that use fixed dates
D = date


# ======================================================================================================
# the reader
# ======================================================================================================
def test_the_standard_format():
    r = parse_prices("Date,Price\n2026-01-02,125.40\n2026-01-05,127.15\n", max_day=FAR)
    assert r.rows == [(D(2026, 1, 2), 125.4), (D(2026, 1, 5), 127.15)]
    assert r.ok and r.clean and r.header and r.delimiter == "," and r.decimal == "." and r.price_column == "Price"
    assert (r.first, r.last) == (D(2026, 1, 2), D(2026, 1, 5)) and r.n_errors == 0 and r.warnings == []
    assert r.describe().startswith("2 price(s) from 2026-01-02 to 2026-01-05")


def test_the_header_is_optional_and_lines_may_be_unsorted_or_commented():
    r = parse_prices("# my broker's table\n2026-01-05, 127.15\n\n2026-01-02, 125.40\n", max_day=FAR)
    assert r.rows == [(D(2026, 1, 2), 125.4), (D(2026, 1, 5), 127.15)] and not r.header


def test_semicolons_and_decimal_commas_the_european_way():
    r = parse_prices("02.01.2026;125,40\n05.01.2026;127,15\n", max_day=FAR)
    assert r.rows == [(D(2026, 1, 2), 125.4), (D(2026, 1, 5), 127.15)]
    assert r.delimiter == ";" and r.decimal == "," and r.date_order == "dmy"
    assert any("could be day-first or month-first" in w for w in r.warnings)       # 02.01 is ambiguous - said so


def test_the_date_order_can_be_chosen_when_the_data_do_not_prove_it():
    text = "02/01/2026;10,5\n05/01/2026;11,5\n"
    assert parse_prices(text, max_day=FAR).rows[0][0] == D(2026, 2, 1)             # slashes: month first by default
    day_first = parse_prices(text, date_order="dmy", max_day=FAR)
    assert [d for d, _ in day_first.rows] == [D(2026, 1, 2), D(2026, 1, 5)] and day_first.warnings == []
    mixed = parse_prices("13.01.2026;10\n01/02/2026;11\n", max_day=FAR)            # 13 proves day first for the file
    assert mixed.date_order == "dmy" and mixed.rows[1][0] == D(2026, 2, 1)


def test_a_day_above_twelve_proves_the_order():
    r = parse_prices("13/01/2026,10\n14/01/2026,11\n", max_day=FAR)
    assert r.date_order == "dmy" and [d for d, _ in r.rows] == [D(2026, 1, 13), D(2026, 1, 14)] and r.warnings == []
    r = parse_prices("01/13/2026,10\n01/14/2026,11\n", max_day=FAR)
    assert r.date_order == "mdy" and [d for d, _ in r.rows] == [D(2026, 1, 13), D(2026, 1, 14)]


def test_a_table_pasted_from_excel_is_tab_separated():
    r = parse_prices("2026-01-02\t1,234.50\n2026-01-05\t1,240.00\n", max_day=FAR)
    assert r.delimiter == "\t" and r.decimal == "." and [v for _, v in r.rows] == [1234.5, 1240.0]


def test_blank_separated_lines_with_words_in_the_date():
    r = parse_prices("Jan 2, 2026 125.40\nJan 5, 2026 127.15\n", max_day=FAR)
    assert r.delimiter == " " and r.rows == [(D(2026, 1, 2), 125.4), (D(2026, 1, 5), 127.15)]


def test_a_yahoo_style_table_uses_the_close_column():
    text = ("Date,Open,High,Low,Close,Adj Close,Volume\n2026-01-02,10,11,9,10.5,10.4,1000\n"
            "2026-01-05,10.5,12,10,11,10.9,2000\n")
    r = parse_prices(text, max_day=FAR)
    assert r.price_column == "Close" and r.rows == [(D(2026, 1, 2), 10.5), (D(2026, 1, 5), 11.0)]


def test_a_header_in_another_language_and_other_column_order():
    r = parse_prices("Kurs;Dátum\n12,5;2026-01-02\n13,0;2026-01-05\n", max_day=FAR)
    assert r.header and r.rows == [(D(2026, 1, 2), 12.5), (D(2026, 1, 5), 13.0)]


def test_more_columns_without_header_use_the_second_and_say_so():
    r = parse_prices("2026-01-02,10.5,extra\n2026-01-05,11.5,extra\n", max_day=FAR)
    assert [v for _, v in r.rows] == [10.5, 11.5]
    assert any("3 columns and no header" in w for w in r.warnings)
    r = parse_prices("2026-01-02,12,5\n2026-01-05,13,5\n", max_day=FAR)               # a decimal comma in a CSV
    assert any("decimal comma" in w for w in r.warnings)


EXCEL_SERIAL = (D(2026, 1, 2) - D(1899, 12, 30)).days


@pytest.mark.parametrize("text", [
    "2026-01-02", "2026/01/02", "2026.01.02.", "2026. 01. 02.", "20260102", "2 Jan 2026", "02-Jan-2026", "2. Jan. 2026",
    "Jan 2, 2026", "January 2nd, 2026", "2026-01-02T16:00:00", "2026-01-02 16:00:00", "02.01.2026", "01/02/2026",
    "02-01-2026", str(EXCEL_SERIAL), f'"{D(2026, 1, 2):%Y-%m-%d}"'])
def test_every_date_format(text):
    assert parse_day(text) == D(2026, 1, 2), text


@pytest.mark.parametrize("text", ["", "abc", "2026-13-45", "31.02.2026", "2026-02-30", "12345678", "99999"])
def test_not_a_date(text):
    assert parse_day(text) is None


@pytest.mark.parametrize("text, decimal, want", [
    ("125.40", ".", 125.4), ("$1,234.50", ".", 1234.5), ("1.234,50", ",", 1234.5), ("12,5", ",", 12.5),
    ("EUR 12.5", ".", 12.5), ("Ft 1 234,5", ",", 1234.5), ("(5.00)", ".", -5.0), ("-5", ".", -5.0),
    ("1,234,567.89", ".", 1234567.89), ("0.125", ".", 0.125), ("abc", ".", None), ("", ".", None), ("-", ".", None)])
def test_every_price_format(text, decimal, want):
    assert parse_price(text, decimal) == want


def test_decimal_mark_and_delimiter_detection():
    assert detect_decimal(["1,234.50", "1,300.00"]) == "." and detect_decimal(["1.234,50", "1.300,00"]) == ","
    assert detect_decimal(["12,5", "13,5"]) == "," and detect_decimal(["12.5", "13.5"]) == "."
    assert detect_decimal(["1,234"], ",") == "." and detect_decimal(["1,234"], ";") == ","       # thousands or decimals?
    assert detect_decimal(["1.234.567"], ",") == "," and detect_decimal(["1,234,567"], ";") == ","
    assert detect_delimiter(["2026-01-02;1,5", "2026-01-03;1,6"]) == ";"
    assert detect_delimiter(["2026-01-02\t1.5", "2026-01-03\t1.6"]) == "\t"
    assert detect_delimiter(["2026-01-02 1.5", "2026-01-03 1.6"]) == " " and detect_delimiter([]) == ","


def test_unusable_lines_are_reported_with_their_line_number_and_the_rest_is_kept():
    text = ("Date,Price\n2026-01-02,10\nnot a date,11\n2026-01-06,abc\n2026-01-07,0\n2026-01-08,-3\n2026-01-09\n"
            "2026-01-12,12\n")
    r = parse_prices(text, max_day=FAR)
    assert r.rows == [(D(2026, 1, 2), 10.0), (D(2026, 1, 12), 12.0)]
    assert r.ok and not r.clean and r.n_errors == 5
    assert r.errors == ["line 3: 'not a date' is not a date", "line 4: 'abc' is not a price",
                        "line 5: the price on 2026-01-07 must be above zero (found 0)",
                        "line 6: the price on 2026-01-08 must be above zero (found -3)",
                        "line 7: expected 'Date, Price', found '2026-01-09'"]


def test_prices_from_the_future_are_rejected():
    r = parse_prices("2026-01-02,10\n2026-07-01,11\n", max_day=D(2026, 6, 30))
    assert r.rows == [(D(2026, 1, 2), 10.0)] and r.errors == ["line 2: 2026-07-01 is in the future"]
    soon = date.today() + timedelta(days=1)                               # the default limit is tomorrow
    assert parse_prices(f"{soon},10").rows == [(soon, 10.0)]
    assert parse_prices(f"{date.today()},10\n{date.today() + timedelta(days=5)},11").n_errors == 1


def test_a_date_before_1970_is_a_typing_error():
    r = parse_prices("2026-01-02,10\n1950-01-02,11\n", max_day=FAR)
    assert r.n_errors == 1 and "before 1970" in r.errors[0]


def test_a_day_that_occurs_twice_keeps_the_last_value():
    r = parse_prices("2026-01-02,10\n2026-01-02,11\n2026-01-05,12\n", max_day=FAR)
    assert r.rows == [(D(2026, 1, 2), 11.0), (D(2026, 1, 5), 12.0)]
    assert r.warnings == ["1 date(s) occur more than once - the last value of each was kept."]


def test_a_large_jump_is_flagged_as_a_possible_split_or_typing_error():
    r = parse_prices("2026-01-02,100\n2026-01-05,10\n2026-01-06,10.5\n", max_day=FAR)
    assert r.rows[1] == (D(2026, 1, 5), 10.0)                              # still imported - the user decides
    assert len(r.warnings) == 1 and "Large jump from 100 on 2026-01-02 to 10 on 2026-01-05" in r.warnings[0]
    assert parse_prices("2026-01-02,100\n2026-01-05,140\n", max_day=FAR).warnings == []


@pytest.mark.parametrize("text", ["", "   \n\n", "# only a comment\n"])
def test_nothing_to_read(text):
    with pytest.raises(PriceParseError, match="Nothing to read"):
        parse_prices(text)


@pytest.mark.parametrize("text, message", [
    ("Date,Price\n", "Only a header line"),
    ("125.4\n126.1\n", "Expected two columns"),
    ("Date,Open,High\n2026-01-02,1,2\n", "Could not find the price column")])
def test_unreadable_tables_explain_what_is_missing(text, message):
    with pytest.raises(PriceParseError, match=message):
        parse_prices(text, max_day=FAR)


def test_files_in_any_common_encoding():
    text = "Dátum;Kurs\n2026-01-02;12,5\n2026-01-05;13,0\n"
    for data in (text.encode("utf-8"), b"\xef\xbb\xbf" + text.encode("utf-8"), text.encode("utf-16"),
                 text.encode("cp1252")):
        r = parse_price_file(data, max_day=FAR)
        assert r.rows == [(D(2026, 1, 2), 12.5), (D(2026, 1, 5), 13.0)] and r.header and r.delimiter == ";", data[:8]
    assert decode_bytes(b"caf\xe9") == "café"


def test_a_binary_file_is_refused_with_a_hint():
    with pytest.raises(PriceParseError, match="binary file"):
        parse_price_file(b"PK\x03\x04" + b"\x00" * 60)


def test_the_text_of_a_series_reads_back_unchanged():
    rows = [(D(2026, 1, 2), 125.4), (D(2026, 1, 5), 127.15234), (D(2026, 1, 6), 1e-3)]
    text = prices_to_text(rows)
    assert text.splitlines()[0] == "Date,Price" and parse_prices(text, max_day=FAR).rows == rows
    assert prices_to_text(rows, ";").startswith("Date;Price\n2026-01-02;125.4\n")


def test_storage_keys():
    assert make_key("SAP", "EUR") == "SAP|EUR" and parse_key("SAP|EUR") == ("SAP", "EUR")
    assert parse_key(BENCHMARK_KEY) == ("@benchmark", "")


# ======================================================================================================
# the merged series
# ======================================================================================================
def test_the_layers_are_merged_by_priority_per_day():
    s = merge_series([(SRC_MANUAL, [(D(2026, 1, 5), 5.0), (D(2026, 1, 2), 2.0)]),
                      (SRC_YAHOO, [(D(2026, 1, 5), 50.0), (D(2026, 1, 6), 6.0)]),
                      (SRC_STATEMENT, [(D(2026, 1, 6), 60.0), (D(2026, 1, 7), 7.0)])], "X")
    assert s.dates == [D(2026, 1, 2), D(2026, 1, 5), D(2026, 1, 6), D(2026, 1, 7)]
    assert s.closes == [2.0, 5.0, 6.0, 7.0] and s.sources == [SRC_MANUAL, SRC_MANUAL, SRC_YAHOO, SRC_STATEMENT]
    assert (s.first, s.last, len(s), s.symbol) == (D(2026, 1, 2), D(2026, 1, 7), 4, "X")
    assert s.count(SRC_MANUAL) == 2 and s.count(SRC_YAHOO) == 1 and s.count(SRC_STATEMENT) == 1
    assert s.asof(D(2026, 1, 4)) == (D(2026, 1, 2), 2.0, SRC_MANUAL) and s.asof(D(2026, 1, 1)) is None
    assert s.asof(D(2030, 1, 1)) == (D(2026, 1, 7), 7.0, SRC_STATEMENT)
    assert merge_series([]) == PriceSeries() and len(merge_series([(SRC_MANUAL, [])])) == 0


# ======================================================================================================
# storage
# ======================================================================================================
KEY = "SAP|EUR"
ROWS = [(D(2025, 3, 3), 1.0), (D(2025, 3, 4), 2.0)]


def test_save_overwrite_replace_and_delete(store):
    assert store.save_manual_prices(KEY, ROWS, "typed rows") == 2
    assert store.get_manual_prices(KEY) == ROWS and store.get_manual_prices("AAPL|USD") == []
    store.save_manual_prices(KEY, [(D(2025, 3, 4), 2.5), (D(2025, 3, 5), 3.0)], "pasted text")
    assert store.get_manual_prices(KEY) == [(D(2025, 3, 3), 1.0), (D(2025, 3, 4), 2.5), (D(2025, 3, 5), 3.0)]
    summary = store.manual_price_summary()[KEY]
    assert (summary["count"], summary["first"], summary["last"]) == (3, D(2025, 3, 3), D(2025, 3, 5))
    assert summary["imported_at"]
    assert store.delete_manual_prices(KEY, [D(2025, 3, 4), D(2025, 3, 9)]) == 1            # only the days that exist
    assert [d for d, _ in store.get_manual_prices(KEY)] == [D(2025, 3, 3), D(2025, 3, 5)]
    store.save_manual_prices(KEY, [(D(2025, 4, 1), 9.0)], replace=True)
    assert store.get_manual_prices(KEY) == [(D(2025, 4, 1), 9.0)]
    assert store.delete_manual_prices(KEY) == 1 and store.manual_price_summary() == {}


def test_each_stock_and_the_benchmark_have_their_own_series(store):
    store.save_manual_prices(KEY, ROWS, "a")
    store.save_manual_prices("AAPL|USD", [(D(2025, 3, 3), 150.0)], "b")
    store.save_manual_prices(BENCHMARK_KEY, [(D(2025, 3, 3), 400.0)], "c")
    assert sorted(store.manual_price_summary()) == ["@benchmark", "AAPL|USD", "SAP|EUR"]
    assert store.delete_manual_prices("AAPL|USD") == 1
    assert store.get_manual_prices(KEY) == ROWS and store.get_manual_prices(BENCHMARK_KEY) == [(D(2025, 3, 3), 400.0)]


def test_manual_prices_survive_a_restart_and_clearing_the_download_cache(tmp_path):
    path = tmp_path / "persist.db"
    Store(path).save_manual_prices(KEY, ROWS, "typed rows")
    again = Store(path)                                                                  # "the app was restarted"
    assert again.get_manual_prices(KEY) == ROWS
    again.upsert_prices("SAP.DE", [(D(2025, 3, 3), 120.0)], "EUR", "SAP SE")
    again.clear_market_cache()
    assert again.get_prices("SAP.DE") == [] and again.get_manual_prices(KEY) == ROWS


# ======================================================================================================
# in the valuation
# ======================================================================================================
D0, END = D(2025, 1, 2), D(2025, 12, 31)


def seed(store, *, aapl=200.0, sap=120.0, eur=0.95, bench=100.0, start=D0):
    """A hand-made download cache of the synthetic account (flat prices - the numbers stay easy to follow)."""
    store.upsert_prices("AAPL", daily(start, END, lambda d: aapl, True), "USD", "Apple")
    store.upsert_prices("SAP.DE", daily(start, END, lambda d: sap, True), "EUR", "SAP SE")
    store.upsert_prices("EUR=X", daily(D0, END, lambda d: eur, True), "EUR", "EUR/USD")
    store.upsert_prices("VUAA.L", daily(D0, END, lambda d: bench, True), "USD", "VUAA")


def point(ser, day):
    return ser.closes[ser.dates.index(day)], ser.sources[ser.dates.index(day)]


def test_manual_prices_fill_the_gaps_and_win_on_a_shared_day(store, service, pf_synth):
    seed(store, start=D(2025, 3, 3))                                                    # downloads start in March
    store.save_manual_prices("AAPL|USD", [(D(2025, 1, 14), 150.0), (D(2025, 2, 3), 151.0), (D(2025, 3, 5), 999.0)])
    md = service.build(pf_synth, DEFAULT_SETTINGS)
    ser = md.price_series["AAPL|USD"]
    assert point(ser, D(2025, 1, 14)) == (150.0, SRC_MANUAL)                            # before the download: filled
    assert point(ser, D(2025, 3, 5)) == (999.0, SRC_MANUAL)                             # both have the day: manual wins
    assert point(ser, D(2025, 3, 6)) == (200.0, SRC_YAHOO) and point(ser, D(2025, 12, 31)) == (200.0, SRC_YAHOO)
    assert md.manual_keys == ["AAPL|USD"] and "SAP|EUR" not in md.manual_keys
    assert md.price("AAPL", "USD") == 200.0 and md.raw_prices["AAPL|USD"].source == "Yahoo Finance"
    assert "manual price history" not in md.mode                                       # the NEWEST price is Yahoo's


def test_a_newer_manual_price_becomes_the_current_price(store, service, pf_synth):
    seed(store)
    store.save_manual_prices("AAPL|USD", [(D(2026, 1, 5), 555.0)])
    md = service.build(pf_synth, DEFAULT_SETTINGS)
    pt = md.raw_prices["AAPL|USD"]
    assert (pt.price, pt.day, pt.source) == (555.0, D(2026, 1, 5), "Manual price history")
    assert md.as_of == D(2026, 1, 5) and md.mode.endswith("+ manual price history for 1 stock(s)")
    m = compute_metrics(pf_synth, md)
    assert m.stocks_usd == pytest.approx(6 * 555.0 + 5 * 120.0 / 0.95)                  # 6 AAPL, 5 SAP.DE shares held
    assert not any("newest price" in n for n in md.notes)                              # SAP is 5 days old: no warning


def test_a_single_price_override_beats_the_history_for_the_current_value_only(store, service, pf_synth):
    seed(store)
    store.save_manual_prices("AAPL|USD", [(D(2026, 1, 5), 555.0)])
    md = service.build(pf_synth, dict(DEFAULT_SETTINGS, price_overrides={"AAPL|USD": 321.0}))
    assert md.price("AAPL", "USD") == 321.0 and md.raw_prices["AAPL|USD"].price == 555.0
    assert md.price_series["AAPL|USD"].closes[-1] == 555.0                              # the history is untouched
    m = compute_metrics(pf_synth, md)
    h = build_history(pf_synth, md, m)
    assert h.last["stocks_usd"] == pytest.approx(6 * 321.0 + 5 * 120.0 / 0.95) == pytest.approx(m.stocks_usd)
    assert h.matches(m) == []


def test_a_stock_without_downloads_is_valued_from_its_manual_prices_alone(store, service, pf_synth):
    seed(store)
    store.delete_manual_prices("SAP|EUR")
    store.clear_market_cache()
    store.upsert_prices("EUR=X", daily(D0, END, lambda d: 0.95, True), "EUR", "EUR/USD")   # FX still downloaded
    store.save_manual_prices("SAP|EUR", daily(D(2025, 1, 15), END, lambda d: 100.0 + d.month, True))
    md = service.build(pf_synth, dict(DEFAULT_SETTINGS, manual_only=["SAP|EUR"]))
    ser = md.price_series["SAP|EUR"]
    assert ser.count(SRC_MANUAL) > 200 and ser.count(SRC_YAHOO) == 0
    assert md.price("SAP", "EUR") == 112.0 and md.raw_prices["SAP|EUR"].source == "Manual price history"
    h = build_history(pf_synth, md)
    cov = h.coverage_for("SAP", "EUR")
    assert cov.manual_points == ser.count(SRC_MANUAL) and cov.fallback_days == 0
    d = D(2025, 6, 17)                                                                  # a Tuesday: month 6 -> 106
    assert h.positions.loc[d.isoformat(), "SAP"] == pytest.approx(5 * 106.0 / 0.95)


def test_manual_prices_rank_above_the_statement_close_on_the_same_day(store, service, pf_synth):
    """Statement closes (12-31) are the last resort: a newer price wins, and on the same day the manual price wins."""
    seed(store)
    store.clear_market_cache()
    store.upsert_prices("EUR=X", daily(D0, END, lambda d: 0.95, True), "EUR", "EUR/USD")
    md = service.build(pf_synth, DEFAULT_SETTINGS)                                      # statement closes only
    assert md.price("SAP", "EUR") == 130.0 and md.raw_prices["SAP|EUR"].source.startswith("IBKR statement")
    store.save_manual_prices("SAP|EUR", [(D(2025, 12, 30), 128.0)])
    md = service.build(pf_synth, DEFAULT_SETTINGS)
    assert md.price("SAP", "EUR") == 130.0                                              # the close of 12-31 is newer
    assert point(md.price_series["SAP|EUR"], D(2025, 12, 30)) == (128.0, SRC_MANUAL)
    store.save_manual_prices("SAP|EUR", [(D(2025, 12, 31), 129.0)])
    md = service.build(pf_synth, DEFAULT_SETTINGS)
    assert md.price("SAP", "EUR") == 129.0 and md.raw_prices["SAP|EUR"].source == "Manual price history"


def test_a_manual_series_that_ends_early_is_flagged_on_the_valuation(store, service):
    """Typed in once and never extended: the stock is valued with an old price, and the valuation says so."""
    data, _ = make_csv(open_positions=False)                                            # no statement closes either
    pf = merge_statements([parse_statement("n.csv", data)])
    seed(store)
    store.save_manual_prices("SAP|EUR", [(D(2025, 12, 1), 125.0)])
    md = service.build(pf, dict(DEFAULT_SETTINGS, manual_only=["SAP|EUR"]))
    assert md.price("SAP", "EUR") == 125.0 and md.as_of == D(2025, 12, 31)
    note = next(n for n in md.notes if n.startswith("SAP: the newest price is from"))
    assert "2025-12-01 (Manual price history), 30 days before the valuation date 2025-12-31" in note
    assert "Market data > Manual price history" in note
    h = build_history(pf, md)
    assert h.coverage_for("SAP", "EUR").stale_days > 0 and any("older than" in n for n in h.notes)


def test_the_benchmark_can_come_from_a_manual_price_history(store, service, pf_synth):
    seed(store)
    store.clear_market_cache()
    store.upsert_prices("EUR=X", daily(D0, END, lambda d: 0.95, True), "EUR", "EUR/USD")
    store.upsert_prices("AAPL", daily(D0, END, lambda d: 200.0, True), "USD", "Apple")
    store.upsert_prices("SAP.DE", daily(D0, END, lambda d: 120.0, True), "EUR", "SAP SE")
    md = service.build(pf_synth, DEFAULT_SETTINGS)                                      # no VUAA.L at all
    assert not md.benchmark.dates and any("No benchmark prices yet" in n for n in md.notes)
    assert compute_metrics(pf_synth, md).benchmark.available is False
    store.save_manual_prices(BENCHMARK_KEY, daily(D0, END, lambda d: 100.0 + 0.1 * (d - D0).days, True))
    md = service.build(pf_synth, DEFAULT_SETTINGS)
    assert md.benchmark.dates[0] == D0 and md.benchmark.dates[-1] == END and md.benchmark.source == "Manual price history"
    assert md.benchmark.latest.price == pytest.approx(100.0 + 0.1 * (END - D0).days)
    assert not any("No benchmark prices yet" in n for n in md.notes)
    m = compute_metrics(pf_synth, md)
    assert m.benchmark.available and m.benchmark.price == pytest.approx(md.benchmark.latest.price)
    h = build_history(pf_synth, md, m)
    assert h.benchmark_available and h.matches(m) == []
    assert h.last["cagr_bench"] == pytest.approx(m.benchmark.cagr_vuaa)
    store.upsert_prices("VUAA.L", daily(D0, END, lambda d: 90.0, True), "USD", "VUAA")        # a download joins in ...
    md = service.build(pf_synth, DEFAULT_SETTINGS)
    assert md.benchmark.source == "Yahoo Finance + manual price history"
    assert md.benchmark.close(D(2025, 6, 2)) == pytest.approx(100.0 + 0.1 * (D(2025, 6, 2) - D0).days)  # ... manual wins


# ======================================================================================================
# end to end through the Tracker
# ======================================================================================================
def make_tracker(home, fail=()):
    t = Tracker(Store(home / "t.db"), FakeProvider(fail=fail, last_day=TODAY), today=lambda: TODAY)
    t.update_settings(export_excel=True, recalc_with_libreoffice=False)
    res = t.import_statement("synthetic.csv", make_csv()[0])
    assert res.ok, res.message
    return t


def test_pasting_prices_for_a_stock_the_provider_does_not_know(home):
    t = make_tracker(home, fail=("SAP.DE",))
    out = t.refresh_prices(ACCOUNT)
    assert "SAP.DE" in out.report.errors and "AAPL" in out.report.fetched                # SAP.DE: Yahoo does not know it
    before = t.snapshot(ACCOUNT)
    assert before.md.price("SAP", "EUR") == 130.0                                       # the statement's closing price
    assert any(n.startswith("SAP: the newest price is from 2025-12-31") for n in before.md.notes)

    parsed = t.parse_manual_prices("Date;Price\n2026-10-28;139,5\n2026-10-29;140,0\n", max_day=FAR)
    assert parsed.clean and parsed.decimal == ","
    assert t.save_manual_prices("SAP|EUR", parsed.rows, source="pasted text") == 2

    after = t.snapshot(ACCOUNT)                                                         # used immediately, no refresh
    assert after.md.price("SAP", "EUR") == 140.0 and after.md.raw_prices["SAP|EUR"].source == "Manual price history"
    fx = after.md.fx_used("EUR")
    assert after.metrics.stocks_usd - before.metrics.stocks_usd == pytest.approx(5 * (140.0 - 130.0) / fx)
    assert not any(n.startswith("SAP: the newest price") for n in after.md.notes)
    h = after.history()
    assert h.coverage_for("SAP", "EUR").manual_points == 2 and h.matches(after.metrics) == []
    assert h.last["stocks_usd"] == pytest.approx(after.metrics.stocks_usd)

    t2 = Tracker(Store(home / "t.db"), FakeProvider(last_day=TODAY), today=lambda: TODAY)       # a new session
    assert t2.manual_prices("SAP|EUR") == parsed.rows and t2.manual_price_summary()["SAP|EUR"]["count"] == 2
    assert t2.snapshot(ACCOUNT).md.price("SAP", "EUR") == 140.0


def test_a_refresh_never_touches_the_manual_prices_and_the_price_cache_clear_keeps_them(home):
    t = make_tracker(home)
    t.save_manual_prices("SAP|EUR", [(D(2025, 7, 1), 111.0)])
    t.refresh_prices(ACCOUNT)
    t.store.clear_market_cache()
    assert t.manual_prices("SAP|EUR") == [(D(2025, 7, 1), 111.0)]
    snap = t.snapshot(ACCOUNT)
    assert snap.md.manual_keys == ["SAP|EUR"] and snap.md.price_series["SAP|EUR"].count(SRC_MANUAL) == 1


def test_the_workbook_shows_the_manual_source_and_the_alias_symbol(home):
    t = make_tracker(home, fail=("SAP.DE",))
    t.refresh_prices(ACCOUNT)
    t.save_manual_prices("SAP|EUR", [(D(2026, 10, 29), 140.0)])
    out = t.export(ACCOUNT)
    assert out.ok, out.errors
    wb = openpyxl.load_workbook(t.workbook_path(t.portfolio(ACCOUNT)))
    ws = wb["Prices"]
    rows = {ws[f"B{r}"].value: r for r in range(1, 60) if ws[f"B{r}"].value in ("AAPL", "SAP")}
    r = rows["SAP"]
    assert (ws[f"D{r}"].value, ws[f"E{r}"].value, ws[f"G{r}"].value) == ("SAP.DE", 140.0, "Manual price history")
    assert str(ws[f"F{r}"].value).startswith("2026-10-29")
    assert ws[f"G{rows['AAPL']}"].value == "Yahoo Finance"
    assert ws[f"B{L.PX_SETTINGS['mode']}"].value.endswith("+ manual price history for 1 stock(s)")


# ======================================================================================================
# the overview tables
# ======================================================================================================
def test_the_overview_of_the_manual_prices(store, pf_synth):
    from ibkr_tracker import tables as T
    store.save_manual_prices("SAP|EUR", [(D(2025, 3, 3), 120.0), (D(2025, 3, 4), 121.0)], "pasted text")
    store.save_manual_prices(BENCHMARK_KEY, [(D(2025, 3, 3), 100.0)], "upload: vuaa.csv")
    df = T.manual_prices_table(pf_synth, store.manual_price_summary(), BENCHMARK_KEY, "VUAA.L")
    assert list(df.columns) == ["Ticker", "Currency", "Held now", "Manual prices", "From", "To", "Source", "Entered"]
    assert df["Ticker"].tolist() == ["AAPL", "SAP", "VUAA.L"] and df["Currency"].tolist() == ["USD", "EUR", "benchmark"]
    assert df["Manual prices"].tolist() == [0, 2, 1]
    assert df["Held now"].tolist() == ["yes", "yes", "-"]
    assert df["From"].tolist() == ["", "2025-03-03", "2025-03-03"] and df["To"].tolist() == ["", "2025-03-04", "2025-03-03"]
    assert df["Source"].tolist() == ["", "pasted text", "upload: vuaa.csv"]
    assert df["Entered"][0] == "" and len(df["Entered"][1]) == 16 and df["Entered"][1][10] == " "      # 2026-11-11 21:42


def test_a_file_of_prices_through_the_tracker(home):
    t = Tracker(Store(home / "t.db"))
    parsed = t.parse_manual_prices_file("Dátum;Kurs\n2026-01-02;12,5\n2026-01-05;13,0\n".encode("cp1252"), max_day=FAR)
    assert parsed.ok and parsed.header and parsed.rows == [(D(2026, 1, 2), 12.5), (D(2026, 1, 5), 13.0)]
    with pytest.raises(PriceParseError):
        t.parse_manual_prices_file(b"PK\x03\x04" + b"\x00" * 40)
