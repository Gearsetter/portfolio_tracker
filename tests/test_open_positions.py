"""The Open Positions section of the latest statement is the source of truth for the share counts.

A stock split changes the number of shares without a trade, so neither the "Corporate Actions" section nor the sum of
the transactions may decide what is held.  ``synth.make_csv(split=...)`` produces exactly that situation: the trades
are as executed (AAPL: +10, -4 = 6 shares) while the Open Positions show 12 shares at half the price.
"""
from __future__ import annotations

from datetime import date

import pytest

from conftest import needs_lo
from helpers import compare_portfolio_sheet, error_cells, layout_for, sheet_values
from synth import make_csv, make_usd_csv
from ibkr_tracker.calc import compute_metrics
from ibkr_tracker.config import DEFAULT_SETTINGS
from ibkr_tracker.excel import build_workbook, embed_values
from ibkr_tracker.market import MarketDataService
from ibkr_tracker.models import statement_from_json, statement_to_json
from ibkr_tracker.parsing import parse_statement
from ibkr_tracker.portfolio import (SRC_OPEN_PLUS_TRADES, SRC_OPEN_POSITIONS, SRC_TRANSACTIONS, merge_statements)
from ibkr_tracker.reconcile import SHARE_CHECK, overall, reconcile
from ibkr_tracker.storage import Store

SPLIT = ("AAPL", 2.0, date(2025, 9, 1))               # 2-for-1 on 2025-09-01
OFFLINE = dict(DEFAULT_SETTINGS, price_source="statement", recalc_with_libreoffice=False)
AAPL, SAP = ("AAPL", "USD"), ("SAP", "EUR")


def _stmt(name, **kw):
    data, truth = make_csv(**kw)
    return parse_statement(name, data), truth


def _md(p, tmp_path):
    return MarketDataService(Store(tmp_path / "t.db")).build(p, OFFLINE)


@pytest.fixture(scope="module")
def split():
    ps, truth = _stmt("split.csv", split=SPLIT)
    return ps, merge_statements([ps]), truth


@pytest.fixture(scope="module")
def split_md(split, tmp_path_factory):
    return _md(split[1], tmp_path_factory.mktemp("split"))


# ======================================================================================================
# the rule
# ======================================================================================================
def test_the_statement_count_beats_the_transaction_sum(split):
    _, p, truth = split
    assert truth.holdings == {AAPL: 12.0, SAP: 5.0} and truth.positions == {AAPL: 6.0, SAP: 5.0}
    assert p.holdings_source == SRC_OPEN_POSITIONS and p.holdings_ref.startswith("split.csv")
    assert p.net_quantities() == truth.holdings                       # what the statement says it holds
    assert p.transaction_quantities() == truth.positions              # what the trades alone would give
    assert p.quantity_differences() == [("AAPL", "USD", 12.0, 6.0)]
    assert p.share_factors() == {AAPL: 2.0, SAP: 1.0}


def test_a_split_is_a_note_not_a_warning(split):
    _, p, _ = split
    assert p.warnings == []
    assert any("AAPL (USD): 12 in the statement, 6 from the transactions" in n for n in p.notes)
    assert any("corporate-action row" in n for n in p.notes)          # the section is acknowledged ...


def test_the_corporate_actions_section_is_not_used_and_not_reported_as_unsupported(split):
    ps, p, truth = split
    assert ps.unsupported == {} and ps.warnings == []
    data, _ = make_csv(split=SPLIT)
    without = "\n".join(l for l in data.decode().splitlines() if not l.startswith("Corporate Actions,")).encode()
    q = merge_statements([parse_statement("no_ca.csv", without)])
    assert q.net_quantities() == p.net_quantities() == truth.holdings  # ... but changes nothing
    assert q.notes != p.notes and not any("corporate-action" in n for n in q.notes)


def test_positions_and_values_follow_the_statement(split, split_md):
    _, p, _ = split
    m = compute_metrics(p, split_md)
    pos = {(x.ticker, x.currency): x for x in m.positions}
    aapl = pos[AAPL]
    assert aapl.quantity == 12.0 and aapl.tx_quantity == 6.0 and aapl.share_factor == 2.0
    assert aapl.price == 80.0 and aapl.value == pytest.approx(960.0)                  # 12 x 80, not 6 x 80
    assert aapl.gross == pytest.approx((10 * 150 + 1) + (-4 * 170 + 1))               # cost = what was actually paid
    assert aapl.ret == pytest.approx(960.0 / 822.0)
    assert pos[SAP].quantity == 5.0 and pos[SAP].share_factor == 1.0
    assert m.stocks_usd == pytest.approx(sum(x.usd_value for x in m.positions))
    # the per-transaction "current total" is expressed in today's shares, so the lots add up to the position
    lots = [r for r in m.trades if r.trade.ticker == "AAPL"]
    assert [r.share_factor for r in lots] == [2.0, 2.0]
    assert [r.current_total for r in lots] == pytest.approx([10 * 2 * 80.0, -4 * 2 * 80.0])
    assert sum(r.current_total for r in lots) == pytest.approx(aapl.value)


def test_cash_and_cost_do_not_depend_on_the_split(split, tmp_path):
    _, p, truth = split
    plain = merge_statements([_stmt("plain.csv")[0]])
    assert p.cash_balances() == plain.cash_balances() and p.cash_balances()["USD"] == pytest.approx(truth.cash["USD"])
    assert [(t.quantity, t.price) for t in p.trades] == [(t.quantity, t.price) for t in plain.trades]


def test_reconciliation_flags_the_difference_as_information(split, split_md):
    _, p, _ = split
    rows = reconcile(p, compute_metrics(p, split_md))
    share = {r.item: r for r in rows if r.check == SHARE_CHECK}
    assert share["AAPL"].status == "INFO" and share["AAPL"].ibkr == 12.0 and share["AAPL"].workbook == 6.0
    assert share["AAPL"].difference == pytest.approx(-6.0)
    assert share["SAP"].status == "OK"
    assert [r.status for r in rows if r.check == "Cash balance"] == ["OK", "OK"]
    assert overall(rows) == "all checks passed"                       # information, not an error


def test_every_statement_position_gets_a_row_even_without_a_trade():
    """A holding that was transferred in (no trade in the archive) is still listed - from the Open Positions."""
    data, _ = make_csv()
    text = data.decode().replace("Open Positions,Data,Summary,Stocks,EUR,SAP,",
                                 "Open Positions,Data,Summary,Stocks,EUR,SAP2,", 1)
    text = "\n".join([text, "Financial Instrument Information,Data,Stocks,SAP2,SAP TWO,1,DE0007164601,SAP2,IBIS,1,COMMON,"])
    p = merge_statements([parse_statement("transfer.csv", text.encode())])
    assert ("SAP2", "EUR") in p.positions_keys() and p.net_quantities()[("SAP2", "EUR")] == 5.0
    assert ("SAP", "EUR") in p.positions_keys()                         # traded, no longer in the statement
    assert p.net_quantities().get(("SAP", "EUR"), 0.0) == 0.0


# ======================================================================================================
# several statements of one account: the chronologically latest one decides
# ======================================================================================================
def test_latest_statement_wins_whatever_the_upload_order_or_file_names():
    old, _ = _stmt("z_old.csv", period_end=date(2025, 6, 30), only_before=date(2025, 7, 1))
    new, truth = _stmt("a_new.csv", period_start=date(2025, 7, 1), only_from=date(2025, 7, 1), split=SPLIT)
    assert merge_statements([old]).net_quantities() == {AAPL: 6.0, SAP: 5.0}          # before the split
    for order in ([old, new], [new, old]):
        p = merge_statements(order)
        assert p.net_quantities() == truth.holdings == {AAPL: 12.0, SAP: 5.0}
        assert p.holdings_ref.startswith("a_new.csv") and p.holdings_source == SRC_OPEN_POSITIONS
        assert len(p.trades) == 3 and p.warnings == []                              # the trades are merged as usual
        assert p.quantity_differences() == [("AAPL", "USD", 12.0, 6.0)]


def test_same_period_csv_and_html_use_the_csv_rendering(pf_both, pf_csv):
    assert pf_both.holdings_ref.startswith("U24013787_20260101_20261023.csv")
    assert pf_both.net_quantities() == pf_csv.net_quantities()


def test_a_position_missing_from_the_latest_statement_is_not_held(tmp_path):
    """The older statement still lists SAP; the latest does not (e.g. acquired for cash by a corporate action).  The
    older snapshot must not leak into the share count, although the trades alone would still show the shares."""
    old, _ = _stmt("old.csv", period_end=date(2025, 3, 31), only_before=date(2025, 4, 1))     # holds AAPL 10 + SAP 5
    data, _ = make_csv(period_start=date(2025, 4, 1))
    text = "\n".join(l for l in data.decode().splitlines()
                     if not l.startswith("Open Positions,Data,Summary,Stocks,EUR,SAP,"))
    new = parse_statement("new.csv", text.encode())
    assert new.has_open_positions and [o.ticker for o in new.open_positions] == ["AAPL"]
    p = merge_statements([old, new])
    assert p.net_quantities() == {AAPL: 6.0}
    assert p.transaction_quantities()[SAP] == 5.0
    assert p.quantity_differences() == [("SAP", "EUR", 0.0, 5.0)] and p.share_factors()[SAP] == 0.0
    m = compute_metrics(p, _md(p, tmp_path))
    sap = next(x for x in m.positions if x.ticker == "SAP")
    assert sap.quantity == 0.0 and sap.value == 0.0 and sap.ret is None                 # closed: no return shown
    assert [r.current_total for r in m.trades if r.trade.ticker == "SAP"] == [0.0]


# ======================================================================================================
# fallbacks (each one is announced by a warning)
# ======================================================================================================
def test_missing_section_in_the_latest_statement_rolls_the_older_one_forward():
    old, _ = _stmt("old.csv", period_end=date(2025, 3, 31), only_before=date(2025, 4, 1))
    new, _ = _stmt("new.csv", period_start=date(2025, 4, 1), only_from=date(2025, 4, 1), open_positions=False)
    assert old.has_open_positions and not new.has_open_positions
    p = merge_statements([old, new])
    assert p.holdings_source == SRC_OPEN_PLUS_TRADES and p.holdings_ref.startswith("old.csv")
    assert p.net_quantities() == {AAPL: 6.0, SAP: 5.0}                  # 10 shares of the old statement - 4 sold in June
    assert any("new.csv has no Open Positions section" in w for w in p.warnings)


def test_no_open_positions_anywhere_falls_back_to_the_transactions(tmp_path):
    ps, truth = _stmt("custom.csv", open_positions=False)
    p = merge_statements([ps])
    assert p.holdings_source == SRC_TRANSACTIONS and p.net_quantities() == truth.positions
    assert any("sum of the transactions" in w for w in p.warnings)
    assert [r for r in reconcile(p, compute_metrics(p, _md(p, tmp_path))) if r.check == SHARE_CHECK] == []
    assert p.share_factors() == {AAPL: 1.0, SAP: 1.0} and p.notes == []


def test_a_flat_account_has_no_section_but_the_nav_proves_it():
    data, _ = make_usd_csv()
    lines = [l for l in data.decode().splitlines()
             if not l.startswith("Open Positions,") and not l.startswith("Net Asset Value,Data,Stock")]
    ps = parse_statement("flat.csv", ("\n".join(lines) + "\n").encode())
    assert not ps.has_open_positions and "stock" not in ps.nav and "total" in ps.nav
    p = merge_statements([ps])
    assert p.holdings_source == SRC_OPEN_POSITIONS and p.holdings == {} and p.warnings == []
    assert p.transaction_quantities()[("MSFT", "USD")] == 6.0           # trades alone would say otherwise


def test_archives_written_before_the_flag_existed_still_load(stmt_csv):
    import json
    ps = parse_statement("c.csv", make_csv(open_positions=False)[0])
    for statement, expected in ((stmt_csv, True), (ps, False)):
        raw = json.loads(statement_to_json(statement))
        raw.pop("open_positions_section")
        raw.pop("notes", None)
        assert statement_from_json(json.dumps(raw)).has_open_positions is expected


# ======================================================================================================
# the workbook
# ======================================================================================================
def test_workbook_cells_for_a_split(split, split_md):
    _, p, _ = split
    wb = build_workbook(p, split_md, OFFLINE)
    ws, lay = wb["Portfolio"], layout_for(p)
    row = {ws[f"J{r}"].value: r for r in range(lay.pos_first, lay.pos_last + 1)}
    r = row["AAPL"]
    assert ws[f"K{r}"].value == 12.0 and ws[f"K{r}"].font.color.rgb.endswith("0000FF")      # statement value, blue
    assert ws[f"S{r}"].value.startswith("=SUMIFS(Trade_Qty") and ws[f"T{r}"].value.startswith("=IF(ABS(S")
    checks = wb["Checks"]
    share = [i for i in range(6, 30) if checks[f"A{i}"].value == SHARE_CHECK]
    assert len(share) == 2 and {checks[f"B{i}"].value: checks[f"C{i}"].value for i in share} == {"AAPL": 12.0, "SAP": 5.0}
    assert all(checks[f"D{i}"].value.startswith("=Portfolio!S") for i in share)
    assert any("Open Positions" in str(c.value) for c in wb["About"]["B"])


def test_workbook_without_open_positions_sums_the_transactions(tmp_path):
    ps, _ = _stmt("custom.csv", open_positions=False)
    p = merge_statements([ps])
    wb = build_workbook(p, _md(p, tmp_path), OFFLINE)
    ws, lay = wb["Portfolio"], layout_for(p)
    assert all(str(ws[f"K{r}"].value).startswith("=SUMIFS(Trade_Qty") for r in range(lay.pos_first, lay.pos_last + 1))
    assert not [i for i in range(6, 30) if wb["Checks"][f"A{i}"].value == SHARE_CHECK]


@needs_lo
def test_recalculated_workbook_matches_the_oracle_after_a_split(split, split_md, tmp_path):
    _, p, _ = split
    path = tmp_path / "split.xlsx"
    build_workbook(p, split_md, OFFLINE).save(path)
    res = embed_values(path)
    assert res.ok and res.errors == 0, res.message
    assert error_cells(path) == []
    vals = sheet_values(path, "Portfolio")
    m = compute_metrics(p, split_md)
    assert compare_portfolio_sheet(vals, p, m) == []
    lay = layout_for(p)
    row = {vals[f"J{r}"]: r for r in range(lay.pos_first, lay.pos_last + 1)}
    r = row["AAPL"]
    assert (vals[f"K{r}"], vals[f"S{r}"], vals[f"T{r}"], vals[f"M{r}"]) == (12, 6, 2, pytest.approx(960.0))
    lots = [lay.t_first + i for i, t in enumerate(p.trades) if t.ticker == "AAPL"]
    assert sum(vals[f"L{i}"] for i in lots) == pytest.approx(vals[f"M{r}"])         # lots add up to the position
    checks = sheet_values(path, "Checks")
    assert checks["B4"] == "all checks passed"
    statuses = {checks[f"B{i}"]: checks[f"G{i}"] for i in range(6, 30) if checks.get(f"A{i}") == SHARE_CHECK}
    assert statuses == {"AAPL": "INFO", "SAP": "OK"}
