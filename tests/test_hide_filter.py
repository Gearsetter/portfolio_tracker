"""The "Hide closed positions and dust balances" view filter.

One setting (``hide_closed_dust``) leaves out of every *list* - dashboard tables and allocation chart, Excel Sections 6
and 7, ``positions.csv`` / ``cash.csv`` - the positions with zero shares and the cash balances whose absolute amount is
below 0.5.  It is display only: totals, returns and CAGR always include everything, and the transaction history, the
reconciliation (Checks sheet) and the market-data tables stay complete.

Ground truth: the synthetic EUR account of ``synth.py`` with ``closed_position=True`` (TSLA: bought 2, sold 2) and
``dust_currency=True`` (two GBP forex trades that leave 0.45 GBP behind).
"""
from __future__ import annotations

import io
from datetime import date

import pandas as pd
import pytest
import streamlit as st
from openpyxl import load_workbook
from streamlit.testing.v1 import AppTest

from conftest import ROOT, TODAY, needs_lo
from fakes import FakeProvider
from helpers import compare_portfolio_sheet, error_cells, layout_for, sheet_values
from synth import ACCOUNT, EUR_PER_USD_CLOSE, GBP_PER_EUR_CLOSE, USD_ACCOUNT, make_csv, make_usd_csv
from ibkr_tracker import tables as T
from ibkr_tracker.calc import compute_metrics
from ibkr_tracker.config import DEFAULT_SETTINGS, hide_closed_dust
from ibkr_tracker.excel import build_workbook, embed_values, open_current, refresh_workbook
from ibkr_tracker.excel import layout as L
from ibkr_tracker.excel.refresh import FILTER_CHANGED
from ibkr_tracker.exports import csv_files
from ibkr_tracker.market import MarketDataService
from ibkr_tracker.market.providers import YahooProvider
from ibkr_tracker.parsing import parse_statement
from ibkr_tracker.portfolio import CONSOLIDATED, DUST_THRESHOLD, consolidate, is_closed, is_dust, merge_statements
from ibkr_tracker.reconcile import SHARE_CHECK, overall, reconcile
from ibkr_tracker.storage import Store
from ibkr_tracker.tracker import Tracker

AAPL, SAP, TSLA = ("AAPL", "USD"), ("SAP", "EUR"), ("TSLA", "USD")
# TSLA has no price in the statement (it is not held any more): a manual price makes the closed lots comparable
PRICES = {"TSLA|USD": 250.0}
OFF = dict(DEFAULT_SETTINGS, price_source="statement", recalc_with_libreoffice=False, price_overrides=PRICES)
ON = dict(OFF, hide_closed_dust=True)
DUST_USD = 0.45 * GBP_PER_EUR_CLOSE / EUR_PER_USD_CLOSE          # 0.45 GBP in USD


def _flt_portfolio(account: str = ACCOUNT, name: str = "filter.csv"):
    data, truth = make_csv(closed_position=True, dust_currency=True)
    return merge_statements([parse_statement(name, data.replace(ACCOUNT.encode(), account.encode()))]), truth


@pytest.fixture(scope="module")
def flt():
    return _flt_portfolio()


@pytest.fixture(scope="module")
def flt_md(flt, tmp_path_factory):
    return MarketDataService(Store(tmp_path_factory.mktemp("flt") / "t.db")).build(flt[0], OFF)


@pytest.fixture(scope="module")
def flt_m(flt, flt_md):
    return compute_metrics(flt[0], flt_md)


def _tag(path) -> str:
    """The layout tag stored in the hidden ``_state`` sheet."""
    return load_workbook(path)["_state"]["B1"].value


def _cells(path, sheet):
    """{coordinate: formula or value} of a sheet of a workbook that has not been recalculated."""
    return {c.coordinate: c.value for row in load_workbook(path)[sheet].iter_rows() for c in row if c.value is not None}


# ======================================================================================================
# the two rules
# ======================================================================================================
def test_the_setting_is_off_by_default_and_read_leniently():
    assert DEFAULT_SETTINGS["hide_closed_dust"] is False
    assert hide_closed_dust(None) is False and hide_closed_dust({}) is False
    assert hide_closed_dust({"hide_closed_dust": True}) is True and hide_closed_dust({"hide_closed_dust": 0}) is False


@pytest.mark.parametrize("quantity, closed", [(0, True), (0.0, True), (1e-9, True), (-1e-9, True),
                                              (0.001, False), (1, False), (-3, False), (0.5, False)])
def test_closed_means_zero_shares(quantity, closed):
    assert is_closed(quantity) is closed


@pytest.mark.parametrize("balance, dust", [(0, True), (0.0, True), (0.45, True), (-0.45, True), (0.4999, True),
                                           (0.5, False), (-0.5, False), (0.5001, False), (12.0, False), (-1234.5, False)])
def test_dust_means_an_absolute_amount_below_half(balance, dust):
    assert DUST_THRESHOLD == 0.5 and is_dust(balance) is dust                # strictly below: 0.50 stays visible


def test_portfolio_lists_without_closed_positions_and_dust(flt):
    p, truth = flt
    assert truth.closed == (TSLA,) and truth.dust == {"GBP": pytest.approx(0.45)}
    assert p.positions_keys() == [AAPL, SAP, TSLA]
    assert p.net_quantities() == {AAPL: 6.0, SAP: 5.0}                       # the statement no longer lists TSLA
    assert p.transaction_quantities()[TSLA] == 0.0                           # ... and the trades add up to zero
    assert p.visible_positions(False) == [AAPL, SAP, TSLA] and p.visible_positions(True) == [AAPL, SAP]
    assert p.currencies() == ["EUR", "USD", "GBP"] and p.cash_balances()["GBP"] == pytest.approx(0.45)
    assert p.visible_currencies(False) == ["EUR", "USD", "GBP"] and p.visible_currencies(True) == ["EUR", "USD"]


def test_the_sample_account_hides_its_empty_usd_and_eur_balances(pf_csv):
    """The real sample account (HUF base): every position is open, but USD and EUR are listed with 0.00."""
    assert pf_csv.currencies() == ["HUF", "USD", "EUR"]
    assert pf_csv.cash_balances()["USD"] == pytest.approx(0.0, abs=1e-6)      # floating point residue, not exactly 0.0
    assert pf_csv.cash_balances()["EUR"] == 0.0 and pf_csv.cash_balances()["HUF"] > 10000
    assert pf_csv.visible_positions(True) == pf_csv.positions_keys() and len(pf_csv.positions_keys()) == 6
    assert pf_csv.visible_currencies(True) == ["HUF"] and pf_csv.visible_currencies(False) == ["HUF", "USD", "EUR"]


# ======================================================================================================
# the numbers: a display filter, never a change of the totals
# ======================================================================================================
def test_totals_returns_and_cagr_always_include_the_hidden_items(flt, flt_md, flt_m):
    m, p = flt_m, flt[0]
    hidden = m.hidden(True)
    assert [r.ticker for r in hidden.closed] == ["TSLA"] and [c.currency for c in hidden.dust] == ["GBP"]
    assert bool(hidden) and not m.hidden(False)
    assert hidden.dust_usd == pytest.approx(DUST_USD) and hidden.closed[0].usd_value == 0.0
    assert m.visible_positions(False) == m.positions and m.visible_cash(False) == m.cash
    assert [r.ticker for r in m.visible_positions(True)] == ["AAPL", "SAP"]
    assert [c.currency for c in m.visible_cash(True)] == ["EUR", "USD"]
    listed = sum(r.usd_value for r in m.visible_positions(True)) + sum(c.usd_value for c in m.visible_cash(True))
    assert m.usd_total == pytest.approx(listed + hidden.dust_usd)                  # the dust is part of the total
    assert m.usd_total == pytest.approx(m.stocks_usd + m.cash_usd)
    assert m.cash_usd == pytest.approx(sum(c.usd_value for c in m.cash)) and len(m.cash) == 3
    # computing the metrics does not depend on the filter at all
    again = compute_metrics(p, flt_md)
    assert (again.usd_total, again.usd_cagr, again.base_total) == (m.usd_total, m.usd_cagr, m.base_total)


def test_reconciliation_stays_complete(flt, flt_m):
    rows = reconcile(flt[0], flt_m)
    assert {r.item for r in rows if r.check == "Cash balance"} == {"EUR", "USD", "GBP"}      # the dust balance too
    assert {r.item for r in rows if r.check == SHARE_CHECK} == {"AAPL", "SAP", "TSLA"}      # the closed position too
    assert overall(rows) == "all checks passed"


# ======================================================================================================
# the GUI tables
# ======================================================================================================
def test_position_and_cash_tables(flt_m):
    full, cut = T.positions_table(flt_m), T.positions_table(flt_m, True)
    assert list(full["Ticker"]) == ["AAPL", "SAP", "TSLA"] and list(cut["Ticker"]) == ["AAPL", "SAP"]
    assert full.loc[full["Ticker"] == "TSLA", "Quantity"].iloc[0] == 0 and list(full.columns) == list(cut.columns)
    assert list(T.cash_table(flt_m)["Currency"]) == ["EUR", "USD", "GBP"]
    assert list(T.cash_table(flt_m, True)["Currency"]) == ["EUR", "USD"]
    assert T.positions_table(flt_m, False).equals(T.positions_table(flt_m))            # off = the old behaviour


def test_hidden_note_names_what_was_left_out(flt_m):
    assert T.hidden_note(flt_m, False) == ""
    both = T.hidden_note(flt_m, True)
    assert "1 closed position" in both and "1 cash balance below 0.5" in both and "still include them" in both
    assert f"{DUST_USD:,.2f} USD" in both
    closed, dust = T.hidden_note(flt_m, True, "closed"), T.hidden_note(flt_m, True, "dust")
    assert "closed position" in closed and "cash" not in closed
    assert "cash balance" in dust and "closed" not in dust


def test_hidden_note_is_empty_when_nothing_is_hidden(tmp_path):
    p = merge_statements([parse_statement("plain.csv", make_csv()[0])])               # nothing closed, nothing small
    m = compute_metrics(p, MarketDataService(Store(tmp_path / "t.db")).build(p, OFF))
    assert T.hidden_note(m, True) == "" and not m.hidden(True)
    assert T.positions_table(m, True).equals(T.positions_table(m)) and T.cash_table(m, True).equals(T.cash_table(m))


def test_summary_table_keeps_every_figure_and_reports_the_hidden_items(flt, flt_m):
    p = flt[0]
    plain, cut = T.summary_table(flt_m, p), T.summary_table(flt_m, p, True)
    assert len(cut) == len(plain) + 2 and cut.iloc[:len(plain)].equals(plain)        # nothing before the note changes
    extra = cut.iloc[len(plain):]
    assert list(extra["Item"]) == ["Hidden closed positions", "Hidden dust cash balances (included in the totals)"]
    assert extra["Value"].iloc[0] == 1 and extra["Value"].iloc[1] == pytest.approx(DUST_USD)
    assert plain.loc[plain["Item"] == "Total value", "Value"].iloc[0] == pytest.approx(flt_m.usd_total)


def test_market_data_does_not_ask_for_the_price_of_a_closed_position(flt, tmp_path):
    p = flt[0]
    md = MarketDataService(Store(tmp_path / "t.db")).build(p, dict(OFF, price_overrides={}))     # no TSLA price at all
    table = T.prices_table(md, p, {})
    src = dict(zip(table["Ticker"], table["Source"]))
    assert src["TSLA"] == "not needed (position closed)" and not src["AAPL"].startswith("MISSING")
    assert sorted(src) == ["AAPL", "SAP", "TSLA"]                                    # still listed: the tab is complete


def test_a_missing_price_is_reported_only_where_it_matters(tmp_path):
    """"No price available" is for stocks that are held: a closed position is worth nothing, and a price the user typed
    in is not missing any more."""
    p = merge_statements([parse_statement("nopx.csv", make_csv(closed_position=True, open_positions=False)[0])])
    svc = MarketDataService(Store(tmp_path / "t.db"))
    base = dict(DEFAULT_SETTINGS, price_source="statement")             # no Open Positions section: no price at all
    notes = " ".join(svc.build(p, base).notes)
    assert "No price available for AAPL (USD)" in notes and "No price available for SAP (EUR)" in notes
    assert "TSLA" not in notes                                          # closed
    notes = " ".join(svc.build(p, dict(base, price_overrides={"AAPL|USD": 150.0})).notes)
    assert "AAPL" not in notes and "No price available for SAP (EUR)" in notes


# ======================================================================================================
# CSV
# ======================================================================================================
@pytest.fixture
def flt_tracker(tracker):
    assert tracker.import_statement("filter.csv", make_csv(closed_position=True, dust_currency=True)[0]).ok
    tracker.update_settings(price_source="statement", price_overrides=PRICES)
    return tracker


def _csv(t, hide: bool, style: str = "international") -> dict[str, pd.DataFrame]:
    snap = t.snapshot(ACCOUNT, dict(t.settings(), price_source="statement", hide_closed_dust=hide))
    return {name: pd.read_csv(io.BytesIO(data), encoding="utf-8-sig", sep=";" if style == "european" else ",",
                              decimal="," if style == "european" else ".")
            for name, data in csv_files(snap, style).items()}


def test_csv_files_leave_out_closed_positions_and_dust(flt_tracker):
    full, cut = _csv(flt_tracker, False), _csv(flt_tracker, True)
    assert list(full["positions.csv"]["Ticker"]) == ["AAPL", "SAP", "TSLA"]
    assert list(cut["positions.csv"]["Ticker"]) == ["AAPL", "SAP"]
    assert list(full["cash.csv"]["Currency"]) == ["EUR", "USD", "GBP"] and list(cut["cash.csv"]["Currency"]) == ["EUR", "USD"]
    # history and reconciliation are complete either way
    for name in ("stock_transactions.csv", "forex.csv", "checks.csv", "dividends.csv", "deposits_withdrawals.csv"):
        assert full[name].equals(cut[name]), name
    assert set(cut["stock_transactions.csv"]["Ticker"]) == {"AAPL", "SAP", "TSLA"}
    assert len(cut["forex.csv"]) == 3                                                # the two GBP trades are still there
    assert {"GBP"} <= set(cut["checks.csv"]["Item"]) and {"TSLA"} <= set(cut["checks.csv"]["Item"])
    # the summary keeps its figures and says what is missing from the lists
    s_full, s_cut = full["summary.csv"], cut["summary.csv"]
    assert len(s_cut) == len(s_full) + 2 and s_cut.iloc[:len(s_full)].equals(s_full)
    assert "Hidden closed positions" in set(s_cut["Item"]) and "Hidden closed positions" not in set(s_full["Item"])


def test_european_csv_follows_the_filter_too(flt_tracker):
    cut = _csv(flt_tracker, True, "european")
    assert list(cut["positions.csv"]["Ticker"]) == ["AAPL", "SAP"] and list(cut["cash.csv"]["Currency"]) == ["EUR", "USD"]
    summary = cut["summary.csv"]                                    # mixed text / numbers: the comma stays text
    dust = summary.loc[summary["Item"] == "Hidden dust cash balances (included in the totals)", "Value"].iloc[0]
    assert float(str(dust).replace(",", ".")) == pytest.approx(DUST_USD)


def test_a_usd_account_loses_its_empty_eur_balance(tracker):
    """The USD-base account has no EUR activity: Section 7 always lists EUR, with 0.00 - hidden as dust."""
    assert tracker.import_statement("usd.csv", make_usd_csv()[0]).ok
    s = dict(tracker.settings(), price_source="statement", hide_closed_dust=True)
    files = csv_files(tracker.snapshot(USD_ACCOUNT, s))
    cash = pd.read_csv(io.BytesIO(files["cash.csv"]), encoding="utf-8-sig")
    positions = pd.read_csv(io.BytesIO(files["positions.csv"]), encoding="utf-8-sig")
    assert list(cash["Currency"]) == ["USD"] and sorted(positions["Ticker"]) == ["KO", "MSFT"]


# ======================================================================================================
# the workbook, recalculated by LibreOffice and compared with the Python oracle
# ======================================================================================================
def _recalculated(p, md, settings, path):
    build_workbook(p, md, settings).save(path)
    res = embed_values(path)
    assert res.ok and res.errors == 0, res.message
    assert error_cells(path) == []
    return sheet_values(path, "Portfolio"), sheet_values(path, "Checks")


@needs_lo
@pytest.mark.parametrize("hide", [False, True])
def test_workbook_matches_the_oracle_with_and_without_the_filter(flt, flt_md, flt_m, tmp_path, hide):
    p = flt[0]
    vals, checks = _recalculated(p, flt_md, ON if hide else OFF, tmp_path / "w.xlsx")
    lay = layout_for(p, hide)
    assert compare_portfolio_sheet(vals, p, flt_m, hide=hide) == []
    assert checks["B4"] == "all checks passed"
    # Sections 8 / 9 / 10 are identical either way: they always cover everything
    assert vals[f"O{lay.sum_usd}"] == pytest.approx(flt_m.usd_total) and vals[f"M{lay.sum_base}"] == pytest.approx(flt_m.base_total)
    tickers = [vals.get(f"J{lay.pos_first + i}") for i in range(lay.n_pos)]
    currencies = [vals.get(f"J{lay.cash_first + i}") for i in range(lay.n_cash)]
    if hide:
        assert (tickers, currencies) == (["AAPL", "SAP"], ["EUR", "USD"]) and (lay.n_pos, lay.n_cash) == (2, 2)
    else:
        assert (tickers, currencies) == (["AAPL", "SAP", "TSLA"], ["EUR", "USD", "GBP"])
    # the transaction history never loses a row
    assert [vals[f"B{lay.t_first + i}"] for i in range(lay.n_trades)] == [r.trade.ticker for r in flt_m.trades]


@needs_lo
def test_the_hidden_dust_is_still_part_of_the_workbook_total(flt, flt_md, flt_m, tmp_path):
    p = flt[0]
    path = tmp_path / "w.xlsx"
    vals, checks = _recalculated(p, flt_md, ON, path)
    lay = layout_for(p, True)
    wb = load_workbook(path)
    formula = wb["Portfolio"][f"O{lay.sum_usd}"].value
    assert formula == f"=SUM(O{lay.pos_first}:O{lay.cash_last})+Hidden_USD"
    assert "Hidden_USD" in wb.defined_names and wb.defined_names["Hidden_USD"].attr_text == "Checks!$K$7"
    # the helper block on the Checks sheet: one row per hidden currency, formula driven, with a total
    assert checks["I1"] == "Hidden cash balances (view filter)" and (checks["I6"], checks["I7"]) == ("GBP", "Total (USD)")
    assert checks["J6"] == pytest.approx(0.45) and checks["K6"] == pytest.approx(DUST_USD) == pytest.approx(checks["K7"])
    assert str(wb["Checks"]["J6"].value).startswith("=SUMIFS(Dep_Amount,Dep_Ccy,$I6)")
    assert wb["Checks"]["K6"].value == "=J6/INDEX(FXL_Used,MATCH($I6,FXL_Ccy,0))"
    # without the dust row the total would be short by exactly that amount
    listed = sum(vals[f"O{r}"] for r in range(lay.pos_first, lay.cash_last + 1) if isinstance(vals.get(f"O{r}"), (int, float)))
    assert vals[f"O{lay.sum_usd}"] == pytest.approx(listed + DUST_USD) == pytest.approx(flt_m.usd_total)


@needs_lo
def test_checks_sheet_stays_complete_when_the_filter_is_on(flt, flt_md, tmp_path):
    p = flt[0]
    path = tmp_path / "w.xlsx"
    _, checks = _recalculated(p, flt_md, ON, path)
    rows = [(checks[f"A{r}"], checks[f"B{r}"], checks[f"G{r}"]) for r in range(6, 20) if checks.get(f"A{r}")]
    assert [x for x in rows if x[0] == "Cash balance"] == [("Cash balance", "EUR", "OK"), ("Cash balance", "USD", "OK"),
                                                             ("Cash balance", "GBP", "OK")]
    assert [x[1:] for x in rows if x[0] == SHARE_CHECK] == [("AAPL", "OK"), ("SAP", "OK"), ("TSLA", "OK")]
    wb = load_workbook(path)
    gbp = next(r for r in range(6, 20) if wb["Checks"][f"B{r}"].value == "GBP")
    tsla = next(r for r in range(6, 20) if wb["Checks"][f"B{r}"].value == "TSLA")
    assert wb["Checks"][f"D{gbp}"].value == "=J6"                                   # the helper block
    assert wb["Checks"][f"D{tsla}"].value == f'=SUMIFS(Trade_Qty,Trade_Ticker,B{tsla},Trade_Ccy,"USD")'


@needs_lo
def test_lots_of_a_hidden_closed_position_keep_their_current_total(flt, flt_md, flt_m, tmp_path):
    """Section 5 looks the share factor up in Section 6 - which does not list TSLA any more."""
    p = flt[0]
    path = tmp_path / "w.xlsx"
    vals, _ = _recalculated(p, flt_md, ON, path)
    lay = layout_for(p, True)
    rows = [(i, r) for i, r in enumerate(flt_m.trades) if r.trade.ticker == "TSLA"]
    assert [round(r.current_total, 6) for _, r in rows] == [500.0, -500.0]
    for i, r in rows:
        assert vals[f"L{lay.t_first + i}"] == pytest.approx(r.current_total) and vals[f"O{lay.t_first + i}"] == pytest.approx(r.usd_current)
    assert "IF(ABS(SUMIFS(Trade_Qty,Trade_Ticker" in str(load_workbook(path)["Portfolio"][f"L{lay.t_first + rows[0][0]}"].value)
    # the lots of the listed positions still use the lookup of Section 6
    aapl = next(i for i, r in enumerate(flt_m.trades) if r.trade.ticker == "AAPL")
    assert load_workbook(path)["Portfolio"][f"L{lay.t_first + aapl}"].value.endswith("SUMIFS(Pos_Factor,Pos_Ticker,B%d,Pos_Ccy,G%d)" % (
        lay.t_first + aapl, lay.t_first + aapl))


@needs_lo
@pytest.mark.parametrize("hide", [False, True])
@pytest.mark.parametrize("scenario, kwargs, n_pos, n_cash", [
    # TSLA is the only stock ever traded and it is closed: with the filter Section 6 has no row at all
    ("every position closed", dict(closed_position=True, only_from=date(2025, 2, 16), only_before=date(2025, 5, 10),
                                   open_positions=False), 0, 2),
    # a liquidated account: no positions, every balance 0.00 - Sections 6 and 7 are both empty
    ("empty account", dict(only_from=date(2026, 1, 1), open_positions=False), 0, 0)])
def test_workbook_with_nothing_left_to_list(tmp_path, hide, scenario, kwargs, n_pos, n_cash):
    p = merge_statements([parse_statement("edge.csv", make_csv(**kwargs)[0])])
    md = MarketDataService(Store(tmp_path / "t.db")).build(p, OFF)
    m = compute_metrics(p, md)
    vals, checks = _recalculated(p, md, ON if hide else OFF, tmp_path / "w.xlsx")
    lay = layout_for(p, hide)
    assert (lay.n_pos, lay.n_cash) == ((n_pos, n_cash) if hide else (len(p.positions_keys()), len(p.currencies())))
    assert compare_portfolio_sheet(vals, p, m, hide=hide) == [] and checks["B4"] == "all checks passed"
    assert vals[f"O{lay.sum_usd}"] == pytest.approx(m.usd_total)
    assert vals[f"K{lay.bm_hdr + 6}"] == pytest.approx(0.0)                    # "Stocks now": nothing is held (not the cash row!)


def test_the_workbook_says_that_the_filter_is_on(flt, flt_md):
    p = flt[0]

    def about(settings):
        ws = build_workbook(p, flt_md, settings)["About"]
        return {r[0].value: r[1].value for r in ws.iter_rows(min_row=3, max_col=2) if r[0].value}

    on, off = about(ON), about(OFF)
    assert on["View filter"].startswith("ON - Sections 6 and 7 leave out 1 closed position(s): TSLA; "
                                        "1 cash balance(s) below 0.5: GBP.")
    assert "still include everything" in on["View filter"] and "View filter" not in off     # silent when it is off


def test_with_nothing_to_hide_the_filter_changes_no_formula(tmp_path):
    """A portfolio without closed positions and dust: the filtered and the normal workbook are cell-for-cell equal."""
    p = merge_statements([parse_statement("plain.csv", make_csv()[0])])
    md = MarketDataService(Store(tmp_path / "t.db")).build(p, OFF)
    assert p.visible_positions(True) == p.positions_keys() and p.visible_currencies(True) == p.currencies()
    a, b = tmp_path / "off.xlsx", tmp_path / "on.xlsx"
    build_workbook(p, md, OFF).save(a)
    build_workbook(p, md, ON).save(b)
    for sheet in ("Portfolio", "Checks", "Prices", "FX", "Benchmark"):
        assert _cells(a, sheet) == _cells(b, sheet), sheet
    assert "Hidden_USD" not in load_workbook(b).defined_names
    assert set(load_workbook(a).defined_names) == set(load_workbook(b).defined_names)
    assert _tag(a) == L.WORKBOOK_VERSION and _tag(b) == L.WORKBOOK_VERSION + "+filter"


# ======================================================================================================
# stale workbooks: the geometry of Sections 6 / 7 depends on the setting
# ======================================================================================================
def test_toggling_the_setting_makes_the_workbook_stale(flt, flt_md, tmp_path):
    p = flt[0]
    path = tmp_path / "w.xlsx"
    build_workbook(p, flt_md, OFF).save(path)
    assert open_current(path, p)[0] is not None and open_current(path, p, False)[0] is not None
    wb, why = open_current(path, p, True)
    assert wb is None and why == FILTER_CHANGED == "the 'Hide closed positions and dust balances' setting changed"
    build_workbook(p, flt_md, ON).save(path)
    assert open_current(path, p, True)[0] is not None
    wb, why = open_current(path, p, False)
    assert wb is None and why == FILTER_CHANGED


def test_a_workbook_of_the_previous_release_stays_current_without_the_filter(flt, flt_md, tmp_path):
    p = flt[0]
    path = tmp_path / "w.xlsx"
    build_workbook(p, flt_md, OFF).save(path)
    assert L.layout_tag(False) == L.WORKBOOK_VERSION == "ibkr-portfolio-tracker/layout-2"       # nothing was appended
    assert L.layout_tag(True) == L.WORKBOOK_VERSION + L.FILTER_TAG
    assert _tag(path) == "ibkr-portfolio-tracker/layout-2"
    assert open_current(path, p)[0] is not None


def test_another_layout_version_wins_over_the_filter_reason(flt, flt_md, tmp_path):
    p = flt[0]
    path = tmp_path / "w.xlsx"
    build_workbook(p, flt_md, ON).save(path)
    wb = load_workbook(path)
    wb["_state"]["B1"] = "ibkr-portfolio-tracker/layout-1" + L.FILTER_TAG
    wb.save(path)
    assert open_current(path, p, True) == (None, "existing file uses another layout version")
    assert open_current(path, p, False) == (None, "existing file uses another layout version")


def test_refresh_rebuilds_on_a_toggle_and_updates_in_place_otherwise(flt, flt_md, tmp_path):
    p = flt[0]
    path = tmp_path / "w.xlsx"
    steps = [(ON, "created"), (ON, "updated"), (OFF, "rebuilt"), (OFF, "updated"), (ON, "rebuilt")]
    for settings, action in steps:
        u = refresh_workbook(path, p, flt_md, settings, recalc=False)
        assert u.action == action, (action, u)
        if action == "rebuilt":
            assert u.reason == FILTER_CHANGED and u.backup == tmp_path / "w.previous.xlsx" and u.backup.exists()
        assert _tag(path) == L.layout_tag(hide_closed_dust(settings))
    assert load_workbook(path)["Portfolio"]["J%d" % layout_for(p, True).pos_first].value == "AAPL"


def test_an_in_place_refresh_with_the_filter_on_touches_only_the_market_sheets(flt, flt_md, tmp_path):
    p = flt[0]
    path = tmp_path / "w.xlsx"
    refresh_workbook(path, p, flt_md, ON, recalc=False)
    before = {s: _cells(path, s) for s in ("Portfolio", "Checks")}
    prices = MarketDataService(Store(tmp_path / "t.db")).build(p, dict(ON, price_overrides={"TSLA|USD": 300.0, "AAPL|USD": 200.0}))
    u = refresh_workbook(path, p, prices, dict(ON, price_overrides={"TSLA|USD": 300.0, "AAPL|USD": 200.0}), recalc=False)
    assert u.action == "updated"
    assert {s: _cells(path, s) for s in ("Portfolio", "Checks")} == before            # formulas and layout untouched
    assert load_workbook(path).defined_names["Hidden_USD"].attr_text == "Checks!$K$7"           # the helper survives
    assert 300.0 in _cells(path, "Prices").values() and 200.0 in _cells(path, "Prices").values()    # new prices arrived


@needs_lo
def test_the_refreshed_filtered_workbook_still_matches_the_oracle(flt, tmp_path):
    p = flt[0]
    path = tmp_path / "w.xlsx"
    settings = dict(ON, price_overrides={"TSLA|USD": 300.0, "AAPL|USD": 200.0})
    store = Store(tmp_path / "t.db")
    refresh_workbook(path, p, MarketDataService(store).build(p, ON), ON, recalc=False)           # first the old prices
    md2 = MarketDataService(store).build(p, settings)
    u = refresh_workbook(path, p, md2, dict(settings, recalc_with_libreoffice=True))
    assert u.action == "updated" and u.recalc is not None and u.recalc.ok and u.recalc.errors == 0
    vals = sheet_values(path, "Portfolio")
    m2 = compute_metrics(p, md2)
    assert compare_portfolio_sheet(vals, p, m2, hide=True) == [] and error_cells(path) == []
    assert sheet_values(path, "Checks")["B4"] == "all checks passed"


# ======================================================================================================
# consolidated view
# ======================================================================================================
@pytest.fixture(scope="module")
def cons():
    eur, _ = _flt_portfolio()
    usd = merge_statements([parse_statement("usd.csv", make_usd_csv()[0])], USD_ACCOUNT)
    return consolidate([eur, usd])


@pytest.fixture(scope="module")
def cons_md(cons, tmp_path_factory):
    return MarketDataService(Store(tmp_path_factory.mktemp("cons") / "t.db")).build(cons, OFF)


def test_consolidated_lists_use_the_merged_figures(cons, cons_md):
    assert cons.visible_positions(True) == sorted([AAPL, SAP, ("MSFT", "USD"), ("KO", "USD")])   # TSLA is closed everywhere
    assert TSLA in cons.positions_keys() and cons.currencies() == ["USD", "EUR", "GBP"]
    assert cons.visible_currencies(True) == ["USD", "EUR"]                       # merged GBP 0.45 is dust
    m = compute_metrics(cons, cons_md)
    assert m.hidden(True).dust_usd == pytest.approx(DUST_USD) and [r.ticker for r in m.hidden(True).closed] == ["TSLA"]
    assert list(T.positions_table(m, True)["Ticker"]) == ["AAPL", "KO", "MSFT", "SAP"]


@needs_lo
def test_the_consolidated_workbook_follows_the_filter(cons, cons_md, tmp_path):
    m = compute_metrics(cons, cons_md)
    vals, checks = _recalculated(cons, cons_md, ON, tmp_path / "all.xlsx")
    assert compare_portfolio_sheet(vals, cons, m, hide=True) == [] and checks["B4"] == "all checks passed"
    lay = layout_for(cons, True)
    assert [vals.get(f"J{lay.pos_first + i}") for i in range(lay.n_pos)] == ["AAPL", "KO", "MSFT", "SAP"]
    assert [vals.get(f"J{lay.cash_first + i}") for i in range(lay.n_cash)] == ["USD", "EUR"]
    assert vals[f"O{lay.sum_usd}"] == pytest.approx(m.usd_total)


# ======================================================================================================
# GUI
# ======================================================================================================
@pytest.fixture
def gui(home, monkeypatch):
    st.cache_resource.clear()
    fake = FakeProvider(last_day=date(2026, 10, 30))
    monkeypatch.setattr(YahooProvider, "history",
                        lambda self, symbol, start, end, include_intraday=False: fake.history(symbol, start, end,
                                                                                               include_intraday))
    t = Tracker(Store(home / "tracker.db"))
    t.update_settings(recalc_with_libreoffice=False, price_source="statement", price_overrides=PRICES)
    assert t.import_statement("filter.csv", make_csv(closed_position=True, dust_currency=True)[0]).ok
    yield t, home
    st.cache_resource.clear()


def _app() -> AppTest:
    return AppTest.from_file(str(ROOT / "app.py"), default_timeout=90)


def _frame(at: AppTest, column: str) -> pd.DataFrame:
    return next(d.value for d in at.dataframe if column in d.value.columns)


def _toggle(at: AppTest):
    return next(w for w in at.sidebar.toggle if w.key == "w_hide")


def _captions(at: AppTest) -> str:
    return " ".join(c.value for c in at.caption)


def test_gui_toggle_hides_closed_positions_and_dust_balances(gui):
    t, home = gui
    at = _app().run()
    assert not at.exception, [e.value for e in at.exception]
    toggle = _toggle(at)
    assert toggle.value is False and "Hide closed positions" in toggle.label and "dust" in toggle.label
    assert list(_frame(at, "USD Gain")["Ticker"]) == ["AAPL", "SAP", "TSLA"]
    assert list(_frame(at, "Net cash")["Currency"]) == ["EUR", "USD", "GBP"]
    assert "Hidden by the filter" not in _captions(at)
    total = {m.label: m.value for m in at.metric}["Total value (USD)"]

    _toggle(at).set_value(True).run()
    assert not at.exception, [e.value for e in at.exception]
    assert t.settings()["hide_closed_dust"] is True                                  # remembered
    assert list(_frame(at, "USD Gain")["Ticker"]) == ["AAPL", "SAP"]
    assert list(_frame(at, "Net cash")["Currency"]) == ["EUR", "USD"]
    captions = _captions(at)
    assert "Hidden by the filter: 1 closed position." in captions and "1 cash balance below 0.5" in captions
    assert {m.label: m.value for m in at.metric}["Total value (USD)"] == total       # the totals do not move
    # the reconciliation and the transaction history are complete
    assert set(_frame(at, "Check")["Item"]) >= {"GBP", "TSLA"}
    assert "TSLA" in set(_frame(at, "Gross")["Ticker"])                              # stock transactions: complete

    _toggle(at).set_value(False).run()
    assert t.settings()["hide_closed_dust"] is False and "Hidden by the filter" not in _captions(at)
    assert list(_frame(at, "Net cash")["Currency"]) == ["EUR", "USD", "GBP"]


def test_gui_toggle_rewrites_the_workbook_and_the_export_tab_explains(gui):
    t, home = gui
    at = _app().run()
    path = home / "output" / f"IBKR_Portfolio_{ACCOUNT}.xlsx"
    assert not path.exists()
    _toggle(at).set_value(True).run()                                                # the export switch is on by default
    assert not at.exception, [e.value for e in at.exception]
    assert path.exists() and _tag(path) == L.layout_tag(True)
    assert "The view filter is on" in _captions(at).replace("**", "")
    _toggle(at).set_value(False).run()
    assert not at.exception and _tag(path) == L.layout_tag(False)
    assert path.with_name(path.stem + ".previous.xlsx").exists()                     # the old file is kept
