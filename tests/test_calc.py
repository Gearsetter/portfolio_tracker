"""The calculation layer on the real sample statement, checked against IBKR's own report figures
(Open Positions, Cash Report, Net Asset Value) and against the definitions of the task."""
from __future__ import annotations

from datetime import date

import pytest

from conftest import TODAY
from fakes import FakeProvider
from ibkr_tracker.calc import _cagr, compute_metrics
from ibkr_tracker.config import DEFAULT_SETTINGS
from ibkr_tracker.market import MarketDataService

# IBKR "Open Positions" of the sample: quantity, close price, value (rounded to cents by IBKR), cost basis
IBKR_POSITIONS = {
    "AMZN": (3.0, 248.0, 744.00, 594.346957),
    "BN": (59.1, 37.97, 2244.03, 2424.486578),
    "GOOGL": (1.7, 320.21, 544.36, 561.092559),
    "META": (6.0, 642.62, 3855.72, 3268.893103),
    "MSFT": (2.0, 524.51, 1049.02, 747.034876),
    "NVO": (30.0, 39.05, 1171.50, 1202.210771),
}
IBKR_NAV_HUF = 3156306.558314319           # "Net Asset Value" total
IBKR_STOCK_HUF = 3145673.2894
IBKR_CASH_HUF = 10633.268914319
IBKR_TRANSFERS_HUF = 2_800_000.0           # "Account Transfers" of the Cash Report
CLOSING_HUF = 327.38                       # period-end HUF per USD in the Mark-to-Market Forex section


@pytest.fixture(scope="module")
def offline_md(pf_csv, tmp_path_factory):
    from ibkr_tracker.storage import Store
    store = Store(tmp_path_factory.mktemp("calc") / "t.db")
    return MarketDataService(store).build(pf_csv, dict(DEFAULT_SETTINGS, price_source="statement"))


@pytest.fixture(scope="module")
def m(pf_csv, offline_md):
    return compute_metrics(pf_csv, offline_md)


# ---- positions (Section 6) ------------------------------------------------------------------------------------
def test_positions_match_ibkr_open_positions(m):
    assert {p.ticker for p in m.positions} == set(IBKR_POSITIONS)
    for p in m.positions:
        qty, close, value, basis = IBKR_POSITIONS[p.ticker]
        assert p.currency == "USD"
        assert p.quantity == pytest.approx(qty, abs=1e-9)
        assert p.price == close
        assert p.value == pytest.approx(value, abs=0.005 + 1e-9)          # IBKR prints the value rounded to cents
        # Gross = sum(Total + Fee) of the position's trades == IBKR's cost basis (6 printed decimals)
        assert p.gross == pytest.approx(basis, abs=1e-5), p.ticker
        assert p.ret == pytest.approx(p.value / p.gross)
        assert p.usd_value == pytest.approx(p.value)                       # USD stock, USD reporting: rate 1
        assert p.usd_ret == pytest.approx(p.usd_value / p.usd_gross)


def test_positions_are_listed_alphabetically_and_cover_every_trade(pf_csv, m):
    assert [p.ticker for p in m.positions] == sorted(p.ticker for p in m.positions)
    assert {(t.ticker, t.currency) for t in pf_csv.trades} == {(p.ticker, p.currency) for p in m.positions}


# ---- cash (Section 7) --------------------------------------------------------------------------------------------
def test_cash_matches_the_ibkr_cash_report(m):
    cash = {c.currency: c for c in m.cash}
    assert [c.currency for c in m.cash] == ["HUF", "USD", "EUR"]            # base first, USD, then the rest
    assert cash["HUF"].balance == pytest.approx(IBKR_CASH_HUF, abs=1e-6)
    assert cash["USD"].balance == pytest.approx(0.0, abs=1e-6)
    assert cash["EUR"].balance == 0.0
    assert cash["HUF"].usd_value == pytest.approx(IBKR_CASH_HUF / CLOSING_HUF)       # latest (period-end) rate


# ---- total value / NAV --------------------------------------------------------------------------------------------
def test_total_value_matches_the_statement_nav(m):
    assert m.base_currency == "HUF" and m.as_of == date(2026, 10, 23)
    # The only difference is IBKR's rounding of the Open Positions values to cents (a couple of forint)
    assert m.base_total == pytest.approx(IBKR_NAV_HUF, abs=5.0)
    assert m.stocks_usd * CLOSING_HUF == pytest.approx(IBKR_STOCK_HUF, abs=5.0)
    assert m.usd_total == pytest.approx(m.stocks_usd + m.cash_usd)
    assert m.base_total == pytest.approx(m.usd_total * CLOSING_HUF)


# ---- deposits / contributions (Section 3) ---------------------------------------------------------------------------
def test_deposits_total_equals_ibkr_account_transfers(m):
    assert len(m.deposits) == 14 and {d.kind for d in m.deposits} == {"deposit"}
    assert m.base_deposits == pytest.approx(IBKR_TRANSFERS_HUF)
    assert sum(d.amount for d in m.deposits) == IBKR_TRANSFERS_HUF


def test_deposit_usd_amounts_divide_by_the_closing_rate_of_the_day(m, offline_md):
    for d in m.deposits:
        rate = offline_md.fx.rate("HUF", d.day)
        assert d.rate == pytest.approx(rate) and d.usd_amount == pytest.approx(d.amount / rate)
        assert 290 < d.rate < 340                                           # HUF per USD in 2026, not USD per HUF
    assert m.usd_deposits == pytest.approx(sum(d.usd_amount for d in m.deposits))


def test_weighted_years_and_cagr(m, offline_md):
    total = sum(d.usd_amount for d in m.deposits)
    years = sum(d.usd_amount * (date(2026, 10, 23) - d.day).days for d in m.deposits) / total / 365.0
    assert m.years == pytest.approx(years) and 0.4 < m.years < 0.5          # money was invested ~5.5 months on average
    assert m.usd_return == pytest.approx(m.usd_total / total)
    assert m.usd_cagr == pytest.approx(m.usd_return ** (1 / years) - 1)
    assert m.base_return == pytest.approx(m.base_total / IBKR_TRANSFERS_HUF)
    assert m.base_cagr == pytest.approx(m.base_return ** (1 / years) - 1)
    assert m.base_cagr > m.usd_cagr                                         # USD/HUF fell, so the HUF result is better


def test_cagr_edge_cases():
    assert _cagr(1.21, 2.0) == pytest.approx(0.10)
    assert _cagr(0.81, 2.0) == pytest.approx(-0.10)
    assert _cagr(None, 1.0) is None and _cagr(1.1, None) is None
    assert _cagr(0.0, 1.0) is None and _cagr(-0.5, 1.0) is None            # no meaningful CAGR for a total loss
    assert _cagr(1.1, 0.0) is None                                          # no time has passed


# ---- transaction rows (Sections 1 / 5) ---------------------------------------------------------------------------------
def test_trade_rows_follow_the_definitions(pf_csv, m, offline_md):
    assert len(m.trades) == len(pf_csv.trades) == 22
    assert [r.trade.dt for r in m.trades] == sorted(r.trade.dt for r in m.trades)          # chronological
    for r in m.trades:
        t = r.trade
        assert r.total == pytest.approx(t.quantity * t.price)
        assert r.gross == pytest.approx(r.total + t.fee) and t.fee > 0                 # fee is a positive cost
        assert r.current_price == offline_md.price(t.ticker, t.currency)
        assert r.current_total == pytest.approx(t.quantity * r.current_price)
        if t.currency == "USD":
            assert r.fx_rate == 1.0
        assert r.usd_total == pytest.approx(r.total / r.fx_rate)
        assert r.usd_gross == pytest.approx(r.gross / r.fx_rate)
        assert r.usd_current == pytest.approx(r.current_total / r.fx_rate)
    assert all(r.trade.quantity > 0 for r in m.trades)                             # the sample only has purchases
    # (sells are negative in every column: covered by the synthetic account in test_synthetic.py)


def test_position_quantities_are_the_sum_of_signed_trades(m):
    for p in m.positions:
        assert p.quantity == pytest.approx(sum(r.trade.quantity for r in m.trades if r.trade.ticker == p.ticker))


# ---- benchmark (VUAA) ------------------------------------------------------------------------------------------------------
def test_benchmark_is_unavailable_without_prices(m):
    b = m.benchmark
    assert not b.available and b.units is None and b.value_usd is None and b.ret_vuaa is None
    assert all(r.vuaa_qty is None and r.bench_close is None for r in m.trades)
    assert b.invested_usd == pytest.approx(sum(r.usd_total for r in m.trades))
    assert b.ret_stocks_latest == pytest.approx(m.stocks_usd / b.invested_usd)


@pytest.fixture(scope="module")
def online(pf_csv, tmp_path_factory):
    from ibkr_tracker.storage import Store
    store = Store(tmp_path_factory.mktemp("online") / "t.db")
    svc = MarketDataService(store, FakeProvider(last_day=TODAY), today=lambda: TODAY)
    svc.refresh(pf_csv, DEFAULT_SETTINGS)
    md = svc.build(pf_csv, DEFAULT_SETTINGS)
    return md, compute_metrics(pf_csv, md)


def test_benchmark_simulation(online):
    md, m = online
    b = m.benchmark
    assert b.available and b.symbol == "VUAA.L"
    for r in m.trades:
        close = md.benchmark.close(r.trade.dt.date())
        assert r.bench_close == close and r.vuaa_qty == pytest.approx(r.usd_total / close)   # sells give negative units
    assert b.units == pytest.approx(sum(r.vuaa_qty for r in m.trades))
    assert b.price == md.benchmark_price() and b.value_usd == pytest.approx(b.units * b.price)
    assert b.invested_usd == pytest.approx(sum(r.usd_total for r in m.trades))
    assert b.ret_vuaa == pytest.approx(b.value_usd / b.invested_usd)
    assert b.excess_usd == pytest.approx(m.stocks_usd - b.value_usd)


def test_vuaa_cagr_is_the_purchase_weighted_time_weighted_cagr(online):
    md, m = online
    b = m.benchmark
    invested = sum(r.usd_total for r in m.trades)
    years = sum(r.usd_total * (md.as_of - r.trade.dt.date()).days for r in m.trades) / invested / 365.0
    assert b.years == pytest.approx(years) and 0 < b.years < 1
    assert b.cagr_vuaa == pytest.approx((b.value_usd / invested) ** (1 / years) - 1)
    assert b.cagr_stocks == pytest.approx((m.stocks_usd / invested) ** (1 / years) - 1)
    # same years and same invested amount on both sides: the CAGR order is the order of the values
    assert (b.cagr_stocks > b.cagr_vuaa) == (m.stocks_usd > b.value_usd) == (b.excess_usd > 0)


def test_vuaa_cagr_needs_benchmark_prices(m):
    b = m.benchmark
    assert b.cagr_vuaa is None                                              # no benchmark prices offline ...
    assert b.years and b.years > 0 and b.cagr_stocks is not None            # ... but the stocks side is still known
    assert b.cagr_stocks == pytest.approx((m.stocks_usd / b.invested_usd) ** (1 / b.years) - 1)


def test_live_prices_change_the_valuation_but_not_the_cost(pf_csv, offline_md, online, m):
    md, live = online
    assert md.as_of == TODAY and live.as_of == TODAY
    for a, b in zip(m.positions, live.positions):
        assert a.ticker == b.ticker and a.gross == pytest.approx(b.gross) and a.quantity == b.quantity
        assert b.price != a.price                                           # Yahoo (fake) vs statement close
    assert live.base_deposits == pytest.approx(m.base_deposits)             # the HUF amounts never depend on market data
    for d in live.deposits:                                                 # ... the USD amounts follow the rate series in use
        assert d.usd_amount == pytest.approx(d.amount / md.fx.rate("HUF", d.day))
    assert live.years > m.years                                             # later valuation date -> longer holding
