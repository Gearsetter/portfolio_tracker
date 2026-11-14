"""Consolidated (all accounts merged) portfolio: combined positions, performance and transaction histories, per-account
breakdown and reconciliation, the Excel workbook and the CSV export - next to the unchanged single-account views.

Ground truth: the synthetic EUR-base account (AAPL 2:1 split, SAP) and the synthetic USD-base account (MSFT, KO) of
``synth.py`` - every expected number is worked out by hand from their ledgers.
"""
from __future__ import annotations

import io
import zipfile
from datetime import date

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from conftest import ROOT, SAMPLE_CSV, TODAY, needs_lo
from fakes import FakeProvider
from helpers import compare_portfolio_sheet, error_cells, layout_for, sheet_values
from synth import ACCOUNT, EUR_PER_USD_CLOSE, USD_ACCOUNT, make_csv, make_usd_csv
from ibkr_tracker.calc import compute_metrics
from ibkr_tracker.config import DEFAULT_SETTINGS
from ibkr_tracker.excel import build_workbook, embed_values
from ibkr_tracker.market import MarketDataService
from ibkr_tracker.market.providers import YahooProvider
from ibkr_tracker.parsing import parse_statement
from ibkr_tracker.portfolio import (CONSOLIDATED, SRC_OPEN_PLUS_TRADES, SRC_OPEN_POSITIONS, SRC_TRANSACTIONS,
                                    consolidate, merge_statements)
from ibkr_tracker.reconcile import SHARE_CHECK, overall, reconcile
from ibkr_tracker.storage import Store
from ibkr_tracker.tracker import Tracker

SPLIT = ("AAPL", 2.0, date(2025, 9, 1))
OFFLINE = dict(DEFAULT_SETTINGS, price_source="statement", recalc_with_libreoffice=False)


def _offline(t: Tracker) -> dict:
    """The tracker's own settings (overrides, base currency, ...) but without Yahoo."""
    return dict(t.settings(), price_source="statement")
AS_OF = date(2025, 12, 31)
USD_PER_EUR_TRADE = 1.03                                 # rate of the EUR account's forex trade (synth.py)


def _eur(account: str = ACCOUNT, name: str = "eur.csv"):
    data, truth = make_csv(split=SPLIT)
    return merge_statements([parse_statement(name, data.replace(ACCOUNT.encode(), account.encode()))]), truth


def _usd(account: str = USD_ACCOUNT, name: str = "usd.csv", period_end: str | None = None):
    data, truth = make_usd_csv()
    data = data.replace(USD_ACCOUNT.encode(), account.encode())
    if period_end:
        data = data.replace(b"December 31, 2025", period_end.encode())
    return merge_statements([parse_statement(name, data)], account), truth


@pytest.fixture(scope="module")
def accounts():
    eur, eur_truth = _eur()
    usd, usd_truth = _usd()
    return eur, usd, eur_truth, usd_truth


@pytest.fixture(scope="module")
def cons(accounts):
    eur, usd, *_ = accounts
    return consolidate([usd, eur])                        # the order of the arguments does not matter


@pytest.fixture(scope="module")
def cons_md(cons, tmp_path_factory):
    return MarketDataService(Store(tmp_path_factory.mktemp("cons") / "t.db")).build(cons, OFFLINE)


@pytest.fixture(scope="module")
def m_all(cons, cons_md):
    return compute_metrics(cons, cons_md)


# ======================================================================================================
# the merged portfolio
# ======================================================================================================
def test_records_of_all_accounts_are_combined_chronologically(accounts, cons):
    eur, usd, *_ = accounts
    assert (cons.account, cons.is_consolidated, cons.account_ids) == (CONSOLIDATED, True, [ACCOUNT, USD_ACCOUNT])
    assert [m.account for m in cons.members] == [ACCOUNT, USD_ACCOUNT]
    assert len(cons.trades) == 6 and len(cons.cash_flows) == 8 and len(cons.dividends) == 4 and len(cons.fx_trades) == 1
    assert [t.dt for t in cons.trades] == sorted(t.dt for t in cons.trades)
    assert {(t.account, t.ticker) for t in cons.trades} == {(ACCOUNT, "AAPL"), (ACCOUNT, "SAP"),
                                                            (USD_ACCOUNT, "MSFT"), (USD_ACCOUNT, "KO")}
    assert all(r.account for r in [*cons.trades, *cons.cash_flows, *cons.dividends, *cons.fx_trades])
    assert cons.fx_trades[0].account == ACCOUNT                         # only the EUR account traded forex
    assert (cons.period_start, cons.period_end) == (date(2025, 1, 1), AS_OF) and len(cons.statements) == 2
    assert cons.warnings == [] and cons.base_currency == "USD"          # EUR and USD accounts -> USD


def test_share_counts_are_the_sum_of_the_accounts_open_positions(accounts, cons):
    _, _, eur_truth, usd_truth = accounts
    assert cons.holdings_source == SRC_OPEN_POSITIONS
    assert cons.net_quantities() == {("AAPL", "USD"): 12.0, ("SAP", "EUR"): 5.0, ("MSFT", "USD"): 6.0, ("KO", "USD"): 20.0}
    assert cons.positions_keys() == sorted(cons.net_quantities())
    assert cons.transaction_quantities()[("AAPL", "USD")] == 6.0         # the split lives in the AAPL lots only
    assert cons.quantity_differences() == [("AAPL", "USD", 12.0, 6.0)]


def test_cash_and_reference_values_are_summed(accounts, cons):
    eur, usd, eur_truth, usd_truth = accounts
    bal = cons.cash_balances()
    assert bal["EUR"] == pytest.approx(eur_truth.cash["EUR"])
    assert bal["USD"] == pytest.approx(eur_truth.cash["USD"] + usd_truth.cash)
    assert cons.reference_cash()["USD"] == pytest.approx(eur.reference_cash()["USD"] + usd.reference_cash()["USD"])
    assert cons.reference_nav() is None                                  # EUR NAV and USD NAV are not comparable here


def test_nothing_is_deduplicated_across_accounts():
    """Two accounts that bought the very same shares at the very same time still own twice as many."""
    a, _ = _usd("U9990002", "a.csv")
    b, _ = _usd("U9990003", "b.csv")
    c = consolidate([a, b])
    assert len(c.trades) == 6 and len(c.cash_flows) == 6 and len(c.dividends) == 2
    assert c.net_quantities() == {("MSFT", "USD"): 12.0, ("KO", "USD"): 40.0}
    assert c.cash_balances()["USD"] == pytest.approx(2 * a.cash_balances()["USD"])
    assert c.base_currency == "USD" and c.reference_nav() == pytest.approx(2 * a.reference_nav())


@pytest.mark.parametrize("override, expected", [(None, "USD"), ("eur", "EUR"), (" HUF ", "HUF")])
def test_base_currency_choice(accounts, override, expected):
    eur, usd, *_ = accounts
    assert consolidate([eur, usd], override).base_currency == expected


def test_common_base_currency_is_kept():
    a, _ = _eur(ACCOUNT, "a.csv")
    b, _ = _eur("U9990009", "b.csv")
    c = consolidate([a, b])
    assert c.base_currency == "EUR" and not any("different base currencies" in n for n in c.notes)
    assert c.cash_balances()["EUR"] == pytest.approx(2 * a.cash_balances()["EUR"])
    assert consolidate([a]).base_currency == "EUR"                       # one account: the view equals that account


def test_different_statement_end_days_are_reported(accounts):
    eur, *_ = accounts
    usd, _ = _usd(period_end="September 30, 2025")
    c = consolidate([eur, usd])
    assert any("end on different days" in w and USD_ACCOUNT in w and "2025-09-30" in w for w in c.warnings)
    assert c.period_end == AS_OF


def test_notes_and_warnings_name_their_account(accounts, cons):
    assert any(n.startswith(f"[{ACCOUNT}]") and "AAPL" in n for n in cons.notes)      # the split note
    assert any("different base currencies (EUR, USD)" in n for n in cons.notes)
    # an account whose statement has no Open Positions section (and no Net Asset Value): its warning names the account
    # and the consolidated share counts are flagged as coming from mixed sources
    _, usd, *_ = accounts
    data, _ = make_csv(open_positions=False)
    plain = merge_statements([parse_statement("plain.csv", data.replace(ACCOUNT.encode(), b"U9990005"))])
    assert plain.holdings_source == SRC_TRANSACTIONS and plain.warnings
    c = consolidate([usd, plain])
    assert c.holdings_source == SRC_OPEN_PLUS_TRADES
    assert any(w.startswith("[U9990005] No statement contains an Open Positions section") for w in c.warnings)
    assert c.net_quantities() == {("MSFT", "USD"): 6.0, ("KO", "USD"): 20.0, ("AAPL", "USD"): 6.0, ("SAP", "EUR"): 5.0}


def test_signature_follows_every_member(accounts, cons):
    eur, usd, *_ = accounts
    again = consolidate([eur, usd])
    assert again.signature() == cons.signature() != eur.signature() != usd.signature()
    other, _ = _usd(name="other.csv", period_end="November 30, 2025")
    assert consolidate([eur, other]).signature() != cons.signature()


# ======================================================================================================
# numbers
# ======================================================================================================
def test_performance_metrics_combine_the_accounts(accounts, cons, cons_md, m_all):
    eur, usd, *_ = accounts
    m_eur, m_usd = compute_metrics(eur, cons_md), compute_metrics(usd, cons_md)
    # additive quantities
    for field in ("usd_total", "stocks_usd", "cash_usd", "usd_deposits"):
        assert getattr(m_all, field) == pytest.approx(getattr(m_eur, field) + getattr(m_usd, field)), field
    sap_usd = 5 * 130.0 / EUR_PER_USD_CLOSE
    total = 960.0 + sap_usd + 9404.27 / EUR_PER_USD_CLOSE + 2271.34 + 4400.0 + 3605.67
    assert m_all.usd_total == pytest.approx(total)
    assert m_all.stocks_usd == pytest.approx(960.0 + sap_usd + 4400.0)
    # contributions of both accounts in USD (EUR deposits at 1.03, interest is not a contribution)
    contributions = {date(2025, 1, 10): 10300.0, date(2025, 3, 5): 5150.0, date(2025, 9, 1): -2060.0,
                     date(2025, 2, 3): -1.545 + 5000.0, date(2025, 6, 2): 3000.0, date(2025, 10, 1): -1000.0}
    dep = sum(contributions.values())
    assert m_all.usd_deposits == pytest.approx(dep) == pytest.approx(20388.455)
    years = sum(v * (AS_OF - d).days for d, v in contributions.items()) / dep / 365.0
    assert m_all.years == pytest.approx(years)
    assert m_all.usd_return == pytest.approx(total / dep)
    assert m_all.usd_cagr == pytest.approx((total / dep) ** (1 / years) - 1)
    # base currency of the consolidated view (USD): the summary in "base" terms equals the USD one
    assert (m_all.base_currency, m_all.as_of) == ("USD", AS_OF)
    assert m_all.base_total == pytest.approx(total) and m_all.base_return == pytest.approx(m_all.usd_return)


def test_positions_of_the_merged_portfolio(m_all):
    pos = {(x.ticker, x.currency): x for x in m_all.positions}
    assert [x.ticker for x in m_all.positions] == ["AAPL", "KO", "MSFT", "SAP"]
    assert pos[("AAPL", "USD")].quantity == 12.0 and pos[("AAPL", "USD")].value == pytest.approx(960.0)
    assert pos[("MSFT", "USD")].quantity == 6.0 and pos[("MSFT", "USD")].value == pytest.approx(3000.0)
    assert pos[("KO", "USD")].value == pytest.approx(1400.0) and pos[("SAP", "EUR")].quantity == 5.0
    assert sum(x.usd_value for x in m_all.positions) == pytest.approx(m_all.stocks_usd)
    assert len(m_all.trades) == 6 and len(m_all.deposits) == 8
    assert {d.account for d in m_all.deposits} == {ACCOUNT, USD_ACCOUNT}


def test_benchmark_invests_every_accounts_purchases(m_all, cons_md):
    b = m_all.benchmark
    assert b.invested_usd == pytest.approx(sum(r.usd_total for r in m_all.trades))
    days = sum(r.usd_total * (AS_OF - r.trade.dt.date()).days for r in m_all.trades) / b.invested_usd
    assert b.years == pytest.approx(days / 365.0)
    assert b.cagr_stocks == pytest.approx((m_all.stocks_usd / b.invested_usd) ** (1 / b.years) - 1)
    assert not b.available and b.cagr_vuaa is None                       # no benchmark prices offline


# ======================================================================================================
# reconciliation
# ======================================================================================================
def test_reconciliation_sums_cash_and_lists_share_counts(cons, m_all):
    rows = reconcile(cons, m_all)
    cash = {r.item: r for r in rows if r.check == "Cash balance"}
    assert set(cash) == {"USD", "EUR"} and all(r.status == "OK" for r in cash.values())
    assert cash["USD"].ibkr == pytest.approx(cons.reference_cash()["USD"])
    share = {r.item: r.status for r in rows if r.check == SHARE_CHECK}
    assert share == {"AAPL": "INFO", "KO": "OK", "MSFT": "OK", "SAP": "OK"}
    assert overall(rows) == "all checks passed"


# ======================================================================================================
# workbook (recalculated by LibreOffice) and CSV
# ======================================================================================================
def test_workbook_has_an_account_column(cons, cons_md):
    wb = build_workbook(cons, cons_md, OFFLINE)
    ws, lay = wb["Portfolio"], layout_for(cons)
    assert ws[f"I{lay.t_hdr}"].value == "Account"
    assert [ws[f"I{lay.t_first + i}"].value for i in range(len(cons.trades))] == [t.account for t in cons.trades]
    assert ws[f"I{lay.d_hdr}"].value == ws[f"I{lay.p_hdr}"].value == ws[f"I{lay.f_hdr}"].value == "Account"
    assert [ws[f"I{lay.p_first + i}"].value for i in range(len(cons.cash_flows))] == [c.account for c in cons.cash_flows]
    about = " ".join(str(c.value) for c in wb["About"]["B"] if c.value)
    assert ACCOUNT in about and USD_ACCOUNT in about and "All accounts (consolidated)" in about
    assert wb.properties.title == "IBKR portfolio All accounts (consolidated)"


def test_single_account_workbooks_have_no_account_column(accounts, tmp_path):
    eur, *_ = accounts
    md = MarketDataService(Store(tmp_path / "t.db")).build(eur, OFFLINE)
    ws = build_workbook(eur, md, OFFLINE)["Portfolio"]
    assert ws[f"I{layout_for(eur).t_hdr}"].value is None


@needs_lo
def test_recalculated_consolidated_workbook_matches_the_oracle(cons, cons_md, m_all, tmp_path):
    path = tmp_path / "all.xlsx"
    build_workbook(cons, cons_md, OFFLINE).save(path)
    res = embed_values(path)
    assert res.ok and res.errors == 0, res.message
    assert error_cells(path) == []
    vals = sheet_values(path, "Portfolio")
    assert compare_portfolio_sheet(vals, cons, m_all) == []
    lay = layout_for(cons)
    assert vals[f"I{lay.t_first}"] == cons.trades[0].account
    checks = sheet_values(path, "Checks")
    assert checks["B4"] == "all checks passed"
    assert sorted(v for k, v in checks.items() if k.startswith("G") and v in ("OK", "INFO")) == \
        ["INFO", "OK", "OK", "OK", "OK", "OK"]


def test_csv_tables_carry_the_account(cons, cons_md, m_all):
    from ibkr_tracker.exports import csv_files, csv_tables, csv_zip
    from ibkr_tracker.tracker import Snapshot
    snap = Snapshot(cons, OFFLINE, cons_md, m_all, reconcile(cons, m_all))
    tables = csv_tables(snap)
    assert {"summary.csv", "positions.csv", "cash.csv", "stock_transactions.csv", "dividends.csv",
            "deposits_withdrawals.csv", "forex.csv", "checks.csv"} <= set(tables)
    assert "accounts.csv" not in tables                                   # filled by the tracker (needs the members)
    trades = tables["stock_transactions.csv"]
    assert list(trades.columns)[:3] == ["Account", "Name", "Ticker"] and len(trades) == 6
    assert set(trades["Account"]) == {ACCOUNT, USD_ACCOUNT}
    assert set(tables["dividends.csv"]["Account"]) == {ACCOUNT, USD_ACCOUNT}
    assert tables["deposits_withdrawals.csv"].shape[0] == 8 and tables["forex.csv"]["Account"].tolist() == [ACCOUNT]
    pos = tables["positions.csv"]
    assert pos.loc[pos["Ticker"] == "AAPL", ["Quantity", "Quantity (transactions)", "Share factor"]].iloc[0].tolist() \
        == [12.0, 6.0, 2.0]
    files = csv_files(snap)
    assert files["positions.csv"].startswith(b"\xef\xbb\xbf")             # UTF-8 byte order mark: Excel-friendly
    with zipfile.ZipFile(io.BytesIO(csv_zip(snap))) as z:
        assert sorted(z.namelist()) == sorted(files)
        back = pd.read_csv(io.BytesIO(z.read("stock_transactions.csv")), encoding="utf-8-sig")
    assert back["Account"].tolist() == trades["Account"].tolist()
    assert back["Total"].tolist() == pytest.approx(trades["Total"].tolist())


# ======================================================================================================
# the service layer
# ======================================================================================================
@pytest.fixture
def two_accounts(home):
    fake = FakeProvider(last_day=TODAY)
    t = Tracker(Store(home / "tracker.db"), fake, today=lambda: TODAY)
    t.update_settings(recalc_with_libreoffice=False)
    assert t.import_statement("eur.csv", make_csv(split=SPLIT)[0]).ok
    assert t.import_statement("usd.csv", make_usd_csv()[0]).ok
    return t, fake, home


def test_tracker_offers_the_consolidated_view_first(two_accounts):
    t, *_ = two_accounts
    assert t.account_choices()[0] == CONSOLIDATED and set(t.account_choices()[1:]) == {ACCOUNT, USD_ACCOUNT}
    assert [a for a, _, _ in t.accounts()] != [CONSOLIDATED]              # the consolidated view is not an account
    t.update_settings(last_account=CONSOLIDATED)
    assert t.default_account() == CONSOLIDATED
    t.update_settings(last_account="U0000000")
    assert t.default_account() in (ACCOUNT, USD_ACCOUNT)
    assert len(t.statements(CONSOLIDATED)) == 2 and len(t.statements(ACCOUNT)) == 1


def test_single_account_views_are_unchanged(two_accounts):
    t, *_ = two_accounts
    p = t.portfolio(ACCOUNT)
    assert not p.is_consolidated and p.account_ids == [ACCOUNT] and p.members == []
    assert t.portfolio("nope") is None
    snap = t.snapshot(USD_ACCOUNT, OFFLINE)
    assert snap.accounts == [] and snap.metrics.usd_total == pytest.approx(8005.67)


def test_snapshot_of_the_consolidated_view(two_accounts):
    t, *_ = two_accounts
    snap = t.snapshot(CONSOLIDATED, OFFLINE)
    m = snap.metrics
    assert snap.problem == "" and snap.portfolio.is_consolidated and m.base_currency == "USD"
    assert [a.account for a in snap.accounts] == [ACCOUNT, USD_ACCOUNT]
    assert sum(a.usd_total for a in snap.accounts) == pytest.approx(m.usd_total)
    assert sum(a.stocks_usd for a in snap.accounts) == pytest.approx(m.stocks_usd)
    assert sum(a.usd_deposits for a in snap.accounts) == pytest.approx(m.usd_deposits)
    assert sum(a.share for a in snap.accounts) == pytest.approx(1.0)
    usd = snap.accounts[1]
    assert usd.usd_total == pytest.approx(8005.67) and usd.base_currency == "USD" and usd.n_trades == 3
    assert set(snap.account_checks) == {ACCOUNT, USD_ACCOUNT}
    assert overall(snap.account_checks[USD_ACCOUNT]) == "all checks passed"
    base = t.snapshot(CONSOLIDATED, dict(OFFLINE, consolidated_base="EUR"))
    assert base.metrics.base_currency == "EUR"
    assert base.metrics.base_total == pytest.approx(m.usd_total * EUR_PER_USD_CLOSE)


def test_consolidated_workbook_is_created_refreshed_in_place_and_separate(two_accounts):
    t, fake, home = two_accounts
    out = t.export(CONSOLIDATED)
    assert out.ok and out.update.action == "created", out.errors
    path = home / "output" / "IBKR_Portfolio_ALL.xlsx"
    assert path == out.update.path and path.exists()
    assert not (home / "output" / f"IBKR_Portfolio_{ACCOUNT}.xlsx").exists()
    p = t.portfolio(CONSOLIDATED)
    assert t.workbook_status(p)[0] == "current"
    mtime = path.stat().st_mtime_ns
    refreshed = t.refresh_prices(CONSOLIDATED)
    assert refreshed.ok and refreshed.update.action == "updated" and path.stat().st_mtime_ns > mtime
    assert [h["as_of"] for h in t.history(CONSOLIDATED)] == [TODAY.isoformat()] and t.history(ACCOUNT) == []
    requested = {c[0] for c in fake.calls}
    assert requested == set(t.service.required_symbols(p, t.settings()))     # every position of every account
    assert {"AAPL", "KO", "MSFT", "EUR=X", "VUAA.L"} <= requested
    # a new statement makes the consolidated workbook stale -> rebuilt, the old one is kept
    assert t.import_statement("usd3.csv", make_usd_csv()[0].replace(USD_ACCOUNT.encode(), b"U9990003")).ok
    assert t.workbook_status(t.portfolio(CONSOLIDATED))[0] == "stale"
    rebuilt = t.export(CONSOLIDATED)
    assert rebuilt.update.action == "rebuilt" and (home / "output" / "IBKR_Portfolio_ALL.previous.xlsx").exists()
    assert t.portfolio(CONSOLIDATED).account_ids == [ACCOUNT, USD_ACCOUNT, "U9990003"]


def test_overrides_apply_to_the_consolidated_view_too(two_accounts):
    t, *_ = two_accounts
    t.update_settings(price_overrides={"MSFT|USD": 600.0})
    snap = t.snapshot(CONSOLIDATED, _offline(t))
    assert next(x for x in snap.metrics.positions if x.ticker == "MSFT").value == pytest.approx(6 * 600.0)


def test_csv_export_of_the_consolidated_view(two_accounts):
    t, _, home = two_accounts
    snap = t.snapshot(CONSOLIDATED, _offline(t))
    files = t.csv_files(snap)
    assert "accounts.csv" in files and "stock_transactions.csv" in files
    accounts = pd.read_csv(io.BytesIO(files["accounts.csv"]), encoding="utf-8-sig")
    assert accounts["Account"].tolist() == [ACCOUNT, USD_ACCOUNT]
    assert accounts["Total value (USD)"].sum() == pytest.approx(snap.metrics.usd_total)
    with zipfile.ZipFile(io.BytesIO(t.csv_zip(snap))) as z:
        assert "accounts.csv" in z.namelist()
    written = t.write_csv(snap)
    folder = home / "output" / "IBKR_Portfolio_ALL_csv"
    assert {w.name for w in written} == set(files) and all(w.parent == folder and w.exists() for w in written)
    single = t.snapshot(USD_ACCOUNT, OFFLINE)
    assert "accounts.csv" not in t.csv_files(single)
    assert "Account" not in pd.read_csv(io.BytesIO(t.csv_files(single)["stock_transactions.csv"]),
                                        encoding="utf-8-sig").columns


def _number(v):
    """A cell of a European CSV read back as text -> float (or the text itself)."""
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return v


def test_european_csv_dialect(two_accounts):
    """Semicolons and decimal commas (Excel under a Hungarian / German ... regional setting) - same numbers."""
    from ibkr_tracker.exports import CSV_STYLES, csv_style
    t, *_ = two_accounts
    assert set(CSV_STYLES) == {"international", "european"} and csv_style({}) == "international"
    assert csv_style({"csv_format": "european"}) == "european" and csv_style({"csv_format": "?"}) == "international"
    assert csv_style({"csv_format": "european"}, "international") == "international"     # explicit style wins
    snap = t.snapshot(CONSOLIDATED, _offline(t))
    intl, euro = t.csv_files(snap), t.csv_files(snap, "european")
    assert set(intl) == set(euro) and "accounts.csv" in euro
    for name in intl:
        assert euro[name].startswith(b"\xef\xbb\xbf") and intl[name].startswith(b"\xef\xbb\xbf")
        a = pd.read_csv(io.BytesIO(intl[name]), encoding="utf-8-sig")
        b = pd.read_csv(io.BytesIO(euro[name]), encoding="utf-8-sig", sep=";", decimal=",")
        assert list(a.columns) == list(b.columns) and len(a) == len(b), name
        for col in a.columns:
            for x, y in zip(a[col], b[col]):
                if pd.isna(x):
                    assert pd.isna(y), (name, col)
                elif isinstance(x, float) or isinstance(y, float) or name == "summary.csv" and col == "Value":
                    assert _number(y) == pytest.approx(_number(x)), (name, col, x, y)
                else:
                    assert x == y, (name, col)
    positions = euro["positions.csv"].decode("utf-8-sig").splitlines()
    assert positions[0].startswith("Ticker;Name;Currency;Quantity;") and positions[1].startswith(
        "AAPL;APPLE INC;USD;12,0;80,0;960,0;")
    assert "Net deposits;20388,455;USD" in euro["summary.csv"].decode("utf-8-sig").splitlines()


def test_csv_dialect_follows_the_setting_and_can_be_overridden(two_accounts):
    t, _, home = two_accounts
    t.update_settings(csv_format="european")
    snap = t.snapshot(CONSOLIDATED, _offline(t))
    assert t.csv_files(snap)["cash.csv"].decode("utf-8-sig").splitlines()[0].count(";") >= 1
    assert ";" not in t.csv_files(snap, "international")["cash.csv"].decode("utf-8-sig")
    with zipfile.ZipFile(io.BytesIO(t.csv_zip(snap))) as z:
        assert ";" in z.read("cash.csv").decode("utf-8-sig")
    written = t.write_csv(snap, "international")
    assert all(";" not in w.read_text(encoding="utf-8-sig").splitlines()[0] for w in written)
    written = t.write_csv(snap)                                                           # setting: european
    assert all(";" in w.read_text(encoding="utf-8-sig").splitlines()[0] for w in written)


# ======================================================================================================
# GUI
# ======================================================================================================
@pytest.fixture
def gui2(home, monkeypatch):
    """Sample HUF account + synthetic USD account (which also holds MSFT: the positions are merged)."""
    st.cache_resource.clear()
    fake = FakeProvider(last_day=date(2026, 10, 30))
    monkeypatch.setattr(YahooProvider, "history",
                        lambda self, symbol, start, end, include_intraday=False: fake.history(symbol, start, end,
                                                                                               include_intraday))
    t = Tracker(Store(home / "tracker.db"))
    t.update_settings(recalc_with_libreoffice=False)
    assert t.import_statement(SAMPLE_CSV.name, SAMPLE_CSV.read_bytes()).ok
    assert t.import_statement("usd.csv", make_usd_csv()[0]).ok
    yield t, fake, home
    st.cache_resource.clear()


def _app() -> AppTest:
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=90)


def _sidebar(at: AppTest, kind: str, key: str):
    return next(w for w in getattr(at.sidebar, kind) if w.key == key)


def test_gui_consolidated_view(gui2):
    t, fake, home = gui2
    at = _app().run()
    assert not at.exception, [e.value for e in at.exception]
    selector = _sidebar(at, "selectbox", "account")
    assert selector.options[0].startswith("∑") and "All accounts (consolidated)" in selector.options[0]
    assert len(selector.options) == 3 and selector.value == t.default_account() != CONSOLIDATED
    single = {m.label: m.value for m in at.metric}["Total value (USD)"]
    labels = [m.label for m in at.metric]
    assert labels.count("Total value (USD)") == 1 and labels.count("Total value (HUF)") == 1   # HUF account: two rows

    selector.set_value(CONSOLIDATED).run()
    assert not at.exception, [e.value for e in at.exception]
    assert t.settings()["last_account"] == CONSOLIDATED                       # remembered
    captions = " ".join(c.value for c in at.caption)
    assert "All accounts (consolidated): U24013787, U9990002" in captions and "base currency USD" in captions
    k = {m.label: m.value for m in at.metric}
    assert k["Total value (USD)"] != single and k["VUAA CAGR (USD)"] == "n/a"
    assert [m.label for m in at.metric].count("Total value (USD)") == 1          # USD base: no repeated base row
    assert any(m.value.strip() == "#### Accounts" for m in at.markdown)
    frames = [d.value for d in at.dataframe if "Share of total" in d.value.columns]
    assert len(frames) == 1 and set(frames[0]["Account"]) == {"U24013787", "U9990002"}
    assert frames[0]["Share of total"].sum() == pytest.approx(100.0)
    total = float(k["Total value (USD)"].split()[0].replace(",", ""))
    assert frames[0]["Total value (USD)"].sum() == pytest.approx(total, abs=0.5)      # the KPI card rounds to whole USD
    positions = next(d.value for d in at.dataframe if "Gain" in d.value.columns)
    assert positions.loc[positions["Ticker"] == "MSFT", "Quantity"].iloc[0] == 8.0       # 2 (HUF account) + 6 (USD)

    sidebar_refresh = _sidebar(at, "button", "refresh")
    sidebar_refresh.click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert (home / "output" / "IBKR_Portfolio_ALL.xlsx").exists()
    assert {m.label: m.value for m in at.metric}["VUAA CAGR (USD)"] not in ("n/a", "")
    assert len(at.get("download_button")) >= 3                                # xlsx, csv zip, single tables
    assert not (home / "output" / "IBKR_Portfolio_U24013787.xlsx").exists()   # per-account workbooks are separate

    _sidebar(at, "selectbox", "account").set_value("U9990002").run()          # the single account is still available
    assert not at.exception and not any(m.value.strip() == "#### Accounts" for m in at.markdown)
    shown = {m.label: m.value for m in at.metric}["Total value (USD)"]
    # valued with the (fake) Yahoo prices downloaded meanwhile: the number of the single account alone
    assert float(shown.split()[0].replace(",", "")) == pytest.approx(t.snapshot("U9990002").metrics.usd_total, abs=0.5)


def test_gui_consolidated_base_currency_setting(gui2):
    t, *_ = gui2
    at = _app().run()
    _sidebar(at, "selectbox", "account").set_value(CONSOLIDATED).run()
    _sidebar(at, "text_input", "w_consbase").set_value("eur").run()
    assert not at.exception and t.settings()["consolidated_base"] == "EUR"
    assert "base currency EUR" in " ".join(c.value for c in at.caption)
    assert any("No FX rate for EUR" in e.value for e in at.error) and not at.metric   # no EUR rate in the statements yet
    _sidebar(at, "button", "refresh").click().run()                                    # the (fake) Yahoo has EUR=X
    assert not at.exception and not at.error
    assert "Total value (EUR)" in {m.label for m in at.metric}
    _sidebar(at, "text_input", "w_consbase").set_value("").run()                      # back to the automatic choice
    assert t.settings()["consolidated_base"] == "" and "base currency USD" in " ".join(c.value for c in at.caption)
    assert "Total value (EUR)" not in {m.label for m in at.metric}


def test_gui_csv_format_choice(gui2):
    t, *_ = gui2
    at = _app().run()
    _sidebar(at, "selectbox", "account").set_value(CONSOLIDATED).run()
    radio = next(r for r in at.radio if r.key == "w_csvformat")
    assert radio.value == "international" and len(radio.options) == 2 and "decimal comma" in radio.options[1]
    radio.set_value("european").run()
    assert not at.exception and t.settings()["csv_format"] == "european"
    assert next(r for r in at.radio if r.key == "w_csvformat").value == "european"
    assert len(at.get("download_button")) >= 3
    next(r for r in at.radio if r.key == "w_csvformat").set_value("international").run()
    assert t.settings()["csv_format"] == "international"
