"""USD-base account without any foreign currency: ``USD.<base>`` is 1, the FX sheet is empty, cash has no EUR/HUF.
All expected numbers are derived by hand from the ledger in ``synth.make_usd_csv``."""
from __future__ import annotations

from datetime import date

import pytest

from conftest import needs_lo
from helpers import compare_portfolio_sheet, error_cells, layout_for, sheet_values
from synth import USD_ACCOUNT, USD_CLOSES, make_usd_csv
from ibkr_tracker.calc import compute_metrics
from ibkr_tracker.config import DEFAULT_SETTINGS
from ibkr_tracker.excel import build_workbook, embed_values
from ibkr_tracker.market import MarketDataService
from ibkr_tracker.parsing import parse_statement
from ibkr_tracker.portfolio import merge_statements
from ibkr_tracker.reconcile import SHARE_CHECK, reconcile
from ibkr_tracker.storage import Store

AS_OF = date(2025, 12, 31)


@pytest.fixture(scope="module")
def data():
    raw, truth = make_usd_csv()
    p = merge_statements([parse_statement("usd.csv", raw)])
    return p, truth


@pytest.fixture(scope="module")
def md(data, tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("usd") / "t.db")
    return MarketDataService(store).build(data[0], dict(DEFAULT_SETTINGS, price_source="statement"))


def test_ledger(data):
    p, t = data
    assert (p.account, p.base_currency) == (USD_ACCOUNT, "USD") and p.warnings == []
    assert p.net_quantities() == {(s, "USD"): q for s, q in t.positions.items()}
    assert p.cash_balances()["USD"] == pytest.approx(t.cash) == pytest.approx(3605.67)
    d = p.dividends[0]
    assert (d.ticker, d.quantity, d.per_share, d.total, round(d.tax_fee, 10)) == ("KO", 20, 0.51, 10.20, 1.53)
    assert [c.kind for c in p.cash_flows] == ["deposit", "deposit", "withdrawal"]
    assert p.fx_trades == [] and p.active_currencies() == ["USD"]


def test_market_data_needs_no_fx(md):
    assert not md.missing_fx and md.as_of == AS_OF
    assert md.fx.currencies() == [] and md.fx_used("USD") == 1.0
    assert md.price("MSFT", "USD") == USD_CLOSES["MSFT"]


def test_metrics(data, md):
    p, t = data
    m = compute_metrics(p, md)
    pos = {x.ticker: x for x in m.positions}
    assert pos["MSFT"].quantity == 6 and pos["MSFT"].value == 3000.0
    assert pos["MSFT"].gross == pytest.approx(t.gross["MSFT"]) == pytest.approx(4000 + 1 - 1800 + 1)
    assert pos["KO"].ret == pytest.approx(1400.0 / 1201.0)
    assert m.usd_total == pytest.approx(t.nav) == pytest.approx(8005.67)
    assert m.base_total == pytest.approx(m.usd_total)                          # base currency is USD: rate 1
    assert [d.rate for d in m.deposits] == [1.0, 1.0, 1.0]
    assert m.usd_deposits == pytest.approx(7000.0) and m.base_deposits == pytest.approx(7000.0)
    years = sum(v * (AS_OF - d).days for d, v in t.contributions.items()) / 7000.0 / 365.0
    assert m.years == pytest.approx(years)
    ret = t.nav / 7000.0
    assert m.usd_return == pytest.approx(ret) and m.usd_cagr == pytest.approx(ret ** (1 / years) - 1)
    assert m.base_cagr == pytest.approx(m.usd_cagr)                            # same currency -> same CAGR


def test_reconciliation_including_nav_is_clean(data, md):
    p, _ = data
    rows = reconcile(p, compute_metrics(p, md))
    assert [r.check for r in rows] == ["Cash balance", SHARE_CHECK, SHARE_CHECK, "Net asset value (USD)"]
    assert all(r.status in ("OK", "INFO") for r in rows), [(r.check, r.item, r.difference) for r in rows]


@needs_lo
def test_workbook_recalculates_to_the_same_numbers(data, md, tmp_path):
    p, t = data
    path = tmp_path / "usd.xlsx"
    build_workbook(p, md, dict(DEFAULT_SETTINGS, price_source="statement")).save(path)
    res = embed_values(path)
    assert res.ok and res.errors == 0, res.message
    assert error_cells(path) == []
    vals = sheet_values(path, "Portfolio")
    assert compare_portfolio_sheet(vals, p, compute_metrics(p, md)) == []
    lay = layout_for(p)
    assert vals[f"M{lay.sum_base}"] == pytest.approx(vals[f"O{lay.sum_usd}"])    # USD summary == base summary
    assert vals[f"C{lay.p_first}"] == 1                                         # "USD.USD" column is 1
    assert sheet_values(path, "Checks")["B4"] == "all checks passed"


def test_vuaa_cagr_by_hand(data, md):
    """Every figure below is worked out on paper from the ledger of ``synth.make_usd_csv`` and a hand-made benchmark."""
    from ibkr_tracker.market.data import BenchSeries
    p, _ = data
    closes = {date(2025, 2, 10): 100.0, date(2025, 3, 12): 110.0, date(2025, 8, 12): 125.0, date(2025, 12, 31): 140.0}
    md.benchmark = BenchSeries("VUAA.L", "USD", list(closes), list(closes.values()), list(closes.values()), "test")
    b = compute_metrics(p, md).benchmark
    # purchases: MSFT +10 @ 400 = 4000, KO +20 @ 60 = 1200, MSFT -4 @ 450 = -1800 (sells are negative)
    invested = 4000 + 1200 - 1800
    units = 4000 / 100 + 1200 / 110 - 1800 / 125                      # 40 + 10.90909.. - 14.4 = 36.50909..
    value = units * 140
    assert (b.invested_usd, b.units, b.value_usd) == (pytest.approx(3400.0), pytest.approx(units), pytest.approx(value))
    assert b.ret_vuaa == pytest.approx(value / invested) == pytest.approx(1.5033155, abs=1e-6)
    # average age of the money: (4000 x 324 + 1200 x 294 - 1800 x 141) / 3400 days to 2025-12-31
    days = (4000 * 324 + 1200 * 294 - 1800 * 141) / 3400.0
    assert (date(2025, 12, 31) - date(2025, 2, 10)).days == 324 and (date(2025, 12, 31) - date(2025, 8, 12)).days == 141
    assert b.years == pytest.approx(days / 365.0) == pytest.approx(1.1241, abs=1e-4)
    assert b.cagr_vuaa == pytest.approx((value / invested) ** (365.0 / days) - 1) == pytest.approx(0.43716, abs=1e-4)
    # stocks: MSFT 6 x 500 + KO 20 x 70 = 4400 USD on the same basis
    assert b.cagr_stocks == pytest.approx((4400 / invested) ** (365.0 / days) - 1)
    assert b.excess_usd == pytest.approx(4400 - value)
