"""Parsers: CSV, HTML, canonical parity, cash tie-out against the IBKR Cash Report."""
from __future__ import annotations

from datetime import date

import pytest

from ibkr_tracker.parsing import detect_format, parse_statement
from ibkr_tracker.parsing.util import (description_key, parse_dt, parse_period, split_description, to_num)

IBKR_ENDING_CASH_HUF = 10633.268914319          # "Ending Cash" of the statement's Cash Report


def test_csv_header_and_counts(stmt_csv):
    s = stmt_csv
    assert (s.account, s.alias, s.base_currency) == ("U24013787", "TBSZ-2026", "HUF")
    assert (s.period_start, s.period_end) == (date(2026, 1, 1), date(2026, 10, 23))
    assert s.fmt == "csv" and s.precision == "full"
    assert len(s.trades) == 22 and len(s.fx_trades) == 62 and len(s.cash_flows) == 14
    assert len(s.open_positions) == 6 and not s.unsupported
    assert s.warnings == []


def test_html_header_and_counts(stmt_html):
    s = stmt_html
    assert (s.account, s.base_currency) == ("U24013787", "HUF")
    assert (s.period_start, s.period_end) == (date(2026, 1, 1), date(2026, 10, 23))
    assert s.fmt == "html" and s.precision == "rounded"
    assert len(s.trades) == 22 and len(s.fx_trades) == 62 and len(s.cash_flows) == 14
    assert not s.unsupported


def test_trade_fields(stmt_csv):
    t = stmt_csv.trades[0] if stmt_csv.trades[0].ticker else None
    by_key = {(x.ticker, x.dt): x for x in stmt_csv.trades}
    msft = by_key[("MSFT", parse_dt("2026-01-29, 11:12:34"))]
    assert (msft.quantity, msft.price, msft.currency) == (0.5, 422.0, "USD")
    assert msft.fee == pytest.approx(0.35035725)          # IBKR prints -0.35035725: stored as a positive cost
    assert msft.name == "MICROSOFT CORP"
    assert all(x.quantity > 0 for x in stmt_csv.trades)   # the sample has only purchases


def test_forex_fee_currency_is_the_base_currency(stmt_csv):
    assert {f.fee_currency for f in stmt_csv.fx_trades} == {"HUF"}
    fee_rows = [f for f in stmt_csv.fx_trades if f.fee]
    assert fee_rows and all(f.fee > 0 for f in fee_rows)  # positive cost


@pytest.mark.parametrize("attr", ["trades", "fx_trades", "cash_flows"])
def test_csv_html_parity(stmt_csv, stmt_html, attr):
    """The HTML rendering is the same statement: identical records (HTML amounts are rounded to cents)."""
    a, b = getattr(stmt_csv, attr), getattr(stmt_html, attr)
    assert len(a) == len(b)
    for x, y in zip(sorted(a, key=lambda r: (r.dt, getattr(r, "quantity", getattr(r, "amount", 0)))),
                    sorted(b, key=lambda r: (r.dt, getattr(r, "quantity", getattr(r, "amount", 0))))):
        assert x.dt == y.dt
        if attr == "cash_flows":
            assert (x.kind, x.currency) == (y.kind, y.currency)
            assert x.amount == pytest.approx(y.amount, abs=0.0051)
        else:
            assert x.quantity == pytest.approx(y.quantity, abs=1e-9)
            assert x.price == pytest.approx(y.price, abs=1e-6)
            assert x.fee == pytest.approx(y.fee, abs=0.0051)
            if attr == "trades":
                assert (x.ticker, x.currency) == (y.ticker, y.currency)
            else:
                assert (x.bought, x.paid) == (y.bought, y.paid)


def test_csv_html_dividend_lines_and_reference_data(stmt_csv, stmt_html):
    key = lambda l: (l.kind, l.ticker, l.dt, l.currency)                       # noqa: E731
    a = {key(l): l.amount for l in stmt_csv.div_lines}
    b = {key(l): l.amount for l in stmt_html.div_lines}
    assert a.keys() == b.keys() and len(a) == 10 + 10 + 2   # 10 dividends + 10 taxes + 2 ADR fees
    for k in a:
        assert a[k] == pytest.approx(b[k], abs=0.0051)
    assert {(p.ticker, p.quantity) for p in stmt_csv.open_positions} == {(p.ticker, p.quantity) for p in stmt_html.open_positions}
    assert stmt_html.nav["total"] == pytest.approx(stmt_csv.nav["total"], abs=0.01)


def test_cash_tie_out_with_ibkr_cash_report(pf_csv, pf_html, pf_both):
    """Σ deposits + dividends - tax - trades - forex ... must reproduce IBKR's Ending Cash to the last digit."""
    assert pf_csv.cash_balances()["HUF"] == pytest.approx(IBKR_ENDING_CASH_HUF, abs=1e-6)
    assert pf_csv.cash_balances()["USD"] == pytest.approx(0.0, abs=1e-6)
    assert pf_both.cash_balances()["HUF"] == pytest.approx(IBKR_ENDING_CASH_HUF, abs=1e-6)   # CSV wins duplicates
    # the HTML rounds every amount to cents: stay within the documented worst-case bound
    n_rounded = len(pf_html.trades) + len(pf_html.fx_trades) + len(pf_html.cash_flows)
    bound = 0.01 + 0.0051 * n_rounded
    assert abs(pf_html.cash_balances()["HUF"] - IBKR_ENDING_CASH_HUF) <= bound
    assert abs(pf_html.cash_balances()["USD"]) <= bound


def test_dividends_are_synchronised(pf_csv):
    divs = pf_csv.dividends
    assert len(divs) == 10
    nvo = [d for d in divs if d.ticker == "NVO"]
    assert len(nvo) == 2
    first = min(nvo, key=lambda d: d.dt)
    assert first.dt == date(2026, 4, 8) and first.quantity == 30
    assert first.per_share == pytest.approx(1.217376)
    assert first.total == pytest.approx(36.52)
    assert first.tax_fee == pytest.approx(9.86 + 0.45)    # DK tax + the ADR fee, both positive costs
    msft = [d for d in divs if d.ticker == "MSFT"]
    assert [d.quantity for d in sorted(msft, key=lambda d: d.dt)] == [0.5, 0.5, 2.0]   # shares held on each date
    assert sum(d.total for d in divs) == pytest.approx(67.17)
    assert sum(d.tax_fee for d in divs) == pytest.approx(17.51 + 0.9)
    assert [d.dt for d in divs] == sorted(d.dt for d in divs)       # chronological


def test_chronological_order(pf_both):
    assert [t.dt for t in pf_both.trades] == sorted(t.dt for t in pf_both.trades)
    assert [f.dt for f in pf_both.fx_trades] == sorted(f.dt for f in pf_both.fx_trades)
    assert [c.dt for c in pf_both.cash_flows] == sorted(c.dt for c in pf_both.cash_flows)


def test_open_positions_match_net_quantities(pf_csv):
    held = {(p.ticker, p.currency): p.quantity for p in pf_csv.latest.open_positions}
    net = {k: v for k, v in pf_csv.net_quantities().items() if abs(v) > 1e-9}
    assert held == pytest.approx(net)


# ---- format detection, helpers, hostile input --------------------------------------------------------------------
def test_detect_format():
    assert detect_format("a.CSV", b"") == "csv"
    assert detect_format("a.htm", b"") == "html"
    assert detect_format("a.html", b"") == "html"
    assert detect_format("noext", b"<!DOCTYPE html><html>") == "html"
    assert detect_format("noext", b"Statement,Header") == "csv"


def test_garbage_is_not_an_account():
    for name, data in (("x.csv", b"foo,bar\n1,2\n"), ("x.htm", b"<html><body>hello</body></html>"), ("x.csv", b"")):
        ps = parse_statement(name, data)
        assert not ps.account


@pytest.mark.parametrize("text,expected", [("1,224", 1224.0), ("-2,421.86", -2421.86), ("(5.0)", -5.0), ("19.2%", 19.2),
                                           ("--", None), ("", None), (None, None), ("abc", None), (3, 3.0)])
def test_to_num(text, expected):
    assert to_num(text) == expected


def test_period_and_description_helpers():
    assert parse_period("January 1, 2026 - October 23, 2026") == (date(2026, 1, 1), date(2026, 10, 23))
    t, isin, _ = split_description("BN (CA11271J1075) Cash Dividend USD 0.07 (Ordinary Dividend)")
    assert (t, isin) == ("BN", "CA11271J1075")
    # dividend / tax / fee lines of one payment share the same key
    base = "NVO(US6701002056) Cash Dividend USD 1.217376 per Share"
    keys = {description_key(f"{base} (Ordinary Dividend)"), description_key(f"{base} - DK Tax"),
            description_key(f"{base} - FEE")}
    assert len(keys) == 1


def test_unsupported_rows_are_reported_not_silently_dropped(synth):
    from synth import make_csv
    data, _ = make_csv()
    text = data.decode() + (
        "Trades,Data,Order,Options,USD,AAPL 250117C00150000,\"2025-02-01, 10:00:00\",1,2.5,2.4,-250,-1,251,0,0,O\n"
        "Crypto Transactions,Header,Currency,Date,Description,Amount\n"
        "Crypto Transactions,Data,BTC,2025-02-02,Bought,0.1\n")
    ps = parse_statement("with_options.csv", text.encode())
    assert "Trades: Options" in ps.unsupported and "Crypto Transactions" in ps.unsupported
    assert any("not supported" in w for w in ps.warnings)
