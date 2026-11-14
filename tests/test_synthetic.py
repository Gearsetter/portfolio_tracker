"""Ground-truth account (sells, withdrawal, monthly fee, interest, EUR stock, EUR base currency, forex commission).

Every expected number below is derived by hand from the ledger in ``synth.py`` - not by the application.
"""
from __future__ import annotations

from datetime import date

import pytest

from conftest import needs_lo
from helpers import compare_portfolio_sheet, error_cells, layout_for, sheet_values
from synth import ACCOUNT, CLOSES, EUR_PER_USD_CLOSE, USD_PER_EUR_TRADE
from ibkr_tracker.calc import compute_metrics
from ibkr_tracker.config import DEFAULT_SETTINGS
from ibkr_tracker.excel import build_workbook, embed_values
from ibkr_tracker.market import MarketDataService
from ibkr_tracker.reconcile import reconcile

EUR_PER_USD_JAN = 1.0 / USD_PER_EUR_TRADE          # offline rate before the first period-end close
AS_OF = date(2025, 12, 31)


@pytest.fixture(scope="module")
def synth_md(pf_synth, tmp_path_factory):
    from ibkr_tracker.storage import Store
    store = Store(tmp_path_factory.mktemp("s") / "t.db")
    return MarketDataService(store).build(pf_synth, dict(DEFAULT_SETTINGS, price_source="statement"))


def test_ledger_is_parsed_exactly(pf_synth, synth):
    _, truth = synth
    p = pf_synth
    assert (p.account, p.base_currency) == (ACCOUNT, "EUR")
    assert len(p.trades) == 3 and len(p.fx_trades) == 1 and len(p.cash_flows) == 5 and len(p.dividends) == 3
    # signs: the sell is negative, fees are positive costs, forex fee is booked in the base currency
    sell = [t for t in p.trades if t.quantity < 0]
    assert len(sell) == 1 and sell[0].quantity == -4 and sell[0].price == 170 and sell[0].fee == 1.0
    fx = p.fx_trades[0]
    assert (fx.bought, fx.paid, fx.quantity, fx.price, fx.fee, fx.fee_currency) == ("EUR", "USD", -3000.0, 1.03, 2.0, "EUR")
    assert [c.kind for c in p.cash_flows] == ["deposit", "monthly fee", "interest", "deposit", "withdrawal"]
    assert p.net_quantities() == truth.positions
    assert p.warnings == []


def test_cash_equals_the_hand_computed_ledger(pf_synth, synth):
    _, truth = synth
    bal = pf_synth.cash_balances()
    assert bal["EUR"] == pytest.approx(truth.cash["EUR"], abs=1e-9) == pytest.approx(9404.27, abs=1e-9)
    assert bal["USD"] == pytest.approx(truth.cash["USD"], abs=1e-9) == pytest.approx(2271.34, abs=1e-9)
    assert list(bal)[:2] == ["EUR", "USD"]                           # base currency first, then USD


def test_dividend_rows(pf_synth):
    d = sorted(pf_synth.dividends, key=lambda x: x.dt)
    assert [(x.ticker, x.quantity, x.per_share, x.total, round(x.tax_fee, 10), x.currency) for x in d] == [
        ("AAPL", 10, 0.25, 2.50, 0.38, "USD"),
        ("SAP", 5, 2.35, 11.75, 3.10, "EUR"),
        ("AAPL", 6, 0.25, 1.50, 0.28, "USD")]              # second AAPL: tax 0.23 + fee 0.05, 6 shares after the sale


def test_reconciliation_is_clean(pf_synth, synth_md):
    m = compute_metrics(pf_synth, synth_md)
    rows = reconcile(pf_synth, m)
    assert rows and all(r.status in ("OK", "INFO") for r in rows), [(r.check, r.item, r.difference) for r in rows]
    assert {r.item for r in rows if r.check == "Cash balance"} == {"EUR", "USD"}


def test_offline_market_data(synth_md):
    md = synth_md
    assert md.price("AAPL", "USD") == 160.0 and md.price("SAP", "EUR") == 130.0
    assert md.as_of == AS_OF
    assert md.fx.rate("EUR", date(2025, 1, 13)) == pytest.approx(EUR_PER_USD_JAN)
    assert md.fx.rate("EUR", date(2025, 1, 2)) == pytest.approx(EUR_PER_USD_JAN)       # before the first rate: first row
    assert md.fx_used("EUR") == pytest.approx(EUR_PER_USD_CLOSE)                        # period-end close of the statement
    assert not md.missing_fx


def test_metrics_against_hand_calculation(pf_synth, synth_md, synth):
    _, truth = synth
    m = compute_metrics(pf_synth, synth_md)
    # ---- Section 1/5 rows --------------------------------------------------------------------------------------
    sell = [r for r in m.trades if r.trade.quantity < 0][0]
    assert sell.total == -680.0 and sell.gross == -679.0 and sell.usd_total == -680.0
    sap = [r for r in m.trades if r.trade.ticker == "SAP"][0]
    assert sap.total == 600.0 and sap.gross == 601.0
    assert sap.fx_rate == pytest.approx(EUR_PER_USD_JAN) and sap.usd_total == pytest.approx(600 * USD_PER_EUR_TRADE)
    # ---- Section 6 positions -----------------------------------------------------------------------------------
    pos = {p.ticker: p for p in m.positions}
    assert pos["AAPL"].quantity == 6 and pos["AAPL"].value == 960.0
    assert pos["AAPL"].gross == pytest.approx(truth.gross[("AAPL", "USD")]) == pytest.approx(822.0)
    assert pos["AAPL"].ret == pytest.approx(960.0 / 822.0) and pos["AAPL"].usd_ret == pytest.approx(960.0 / 822.0)
    assert pos["SAP"].value == 650.0 and pos["SAP"].ret == pytest.approx(650.0 / 601.0)
    sap_usd = 650.0 / EUR_PER_USD_CLOSE
    assert pos["SAP"].usd_value == pytest.approx(sap_usd)
    assert pos["SAP"].usd_ret == pytest.approx(sap_usd / (601.0 * USD_PER_EUR_TRADE))   # cost converted at the buy date
    # ---- Section 7 cash ------------------------------------------------------------------------------------------
    cash = {c.currency: c for c in m.cash}
    assert cash["EUR"].balance == pytest.approx(9404.27) and cash["EUR"].usd_value == pytest.approx(9404.27 / EUR_PER_USD_CLOSE)
    assert cash["USD"].balance == pytest.approx(2271.34) and cash["USD"].usd_value == pytest.approx(2271.34)
    # ---- Section 3: USD amounts of the deposits (rate = last close on/before the day, first row before the data) ----
    usd = {(d.kind, d.day): d.usd_amount for d in m.deposits}
    assert usd[("deposit", date(2025, 1, 10))] == pytest.approx(10000 * USD_PER_EUR_TRADE)
    assert usd[("deposit", date(2025, 3, 5))] == pytest.approx(5000 * USD_PER_EUR_TRADE)
    assert usd[("withdrawal", date(2025, 9, 1))] == pytest.approx(-2000 * USD_PER_EUR_TRADE)
    assert usd[("monthly fee", date(2025, 2, 3))] == pytest.approx(-1.5 * USD_PER_EUR_TRADE)
    # ---- Section 8 / 9 ---------------------------------------------------------------------------------------------
    contributions = {date(2025, 1, 10): 10300.0, date(2025, 3, 5): 5150.0, date(2025, 9, 1): -2060.0,
                     date(2025, 2, 3): -1.545}                                     # interest is NOT a contribution
    dep_usd = sum(contributions.values())
    assert m.usd_deposits == pytest.approx(dep_usd) == pytest.approx(13388.455)
    total_usd = 960.0 + sap_usd + 9404.27 / EUR_PER_USD_CLOSE + 2271.34
    assert m.usd_total == pytest.approx(total_usd)
    assert m.stocks_usd == pytest.approx(960.0 + sap_usd) and m.cash_usd == pytest.approx(total_usd - 960.0 - sap_usd)
    assert m.usd_return == pytest.approx(total_usd / dep_usd)
    years = sum(v * (AS_OF - d).days for d, v in contributions.items()) / dep_usd / 365.0
    assert m.years == pytest.approx(years)
    assert m.usd_cagr == pytest.approx((total_usd / dep_usd) ** (1 / years) - 1)
    base_total = total_usd * EUR_PER_USD_CLOSE
    assert m.base_total == pytest.approx(base_total)
    assert m.base_deposits == pytest.approx(10000 + 5000 - 2000 - 1.5) == pytest.approx(truth.deposits_eur)
    assert m.base_return == pytest.approx(base_total / 12998.5)
    assert m.base_cagr == pytest.approx((base_total / 12998.5) ** (1 / years) - 1)


def test_interest_is_not_a_contribution(pf_synth, synth_md):
    m = compute_metrics(pf_synth, synth_md)
    with_interest = sum(d.usd_amount for d in m.deposits)
    assert with_interest - m.usd_deposits == pytest.approx(0.12 * USD_PER_EUR_TRADE)


# ---- the workbook (recalculated by LibreOffice) must agree with all of the above -------------------------------------
@needs_lo
def test_workbook_formulas_reproduce_the_numbers(pf_synth, synth_md, synth, tmp_path):
    _, truth = synth
    settings = dict(DEFAULT_SETTINGS, price_source="statement")
    path = tmp_path / "synthetic.xlsx"
    build_workbook(pf_synth, synth_md, settings).save(path)
    res = embed_values(path)
    assert res.ok and res.errors == 0, res.message
    assert error_cells(path) == []
    vals = sheet_values(path, "Portfolio")
    m = compute_metrics(pf_synth, synth_md)
    assert compare_portfolio_sheet(vals, pf_synth, m) == []
    lay = layout_for(pf_synth)
    assert vals[f"J{lay.cash_first}"] == "EUR" and vals[f"K{lay.cash_first}"] == pytest.approx(truth.cash["EUR"], abs=1e-8)
    assert vals[f"K{lay.cash_first + 1}"] == pytest.approx(truth.cash["USD"], abs=1e-8)
    checks = sheet_values(path, "Checks")
    assert checks["B4"] == "all checks passed"
    statuses = [v for k, v in checks.items() if k.startswith("G") and k != "G5"]
    assert statuses and set(statuses) <= {"OK", "INFO"}
