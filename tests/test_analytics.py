"""Risk metrics (``ibkr_tracker.analytics``): every figure is checked against an independent hand / numpy calculation on
a hand-built day-by-day history - no provider, no store, no network."""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from ibkr_tracker import analytics as an
from ibkr_tracker import tables as T
from ibkr_tracker.history import History
from ibkr_tracker.market.data import BenchSeries, FxTable, MarketData, PriceSeries, SRC_STATEMENT, SRC_YAHOO

MON = date(2025, 1, 6)                                    # a Monday


# ---------------------------------------------------------------------------------------------------------------
# hand-made histories and market data
# ---------------------------------------------------------------------------------------------------------------
def bare_md(bench: list[tuple[date, float]] | None = None, price_series: dict | None = None,
            bench_over: float | None = None) -> MarketData:
    b = BenchSeries("BENCH", "USD", [d for d, _ in bench or []], [v for _, v in bench or []],
                    [v for _, v in bench or []], "test")
    return MarketData(prices={}, raw_prices={}, price_overrides={}, fx=FxTable({}), fx_latest_raw={}, fx_overrides={},
                      benchmark=b, benchmark_override=bench_over, as_of=date(2025, 12, 31), mode="test",
                      price_series=price_series or {})


def make_history(points: dict[date, tuple[float, float]], *, base: str = "USD", fx: dict[date, float] | None = None,
                 positions: pd.DataFrame | None = None, keys: dict | None = None) -> History:
    """``points``: ``{day: (total value USD, net deposits USD)}`` on the days that have a value; the days in between
    carry the last value forward (weekends, holidays) - the way the real history does."""
    first, last = min(points), max(points)
    idx = pd.date_range(first, last, freq="D", name="Date")
    value = pd.Series({pd.Timestamp(d): v[0] for d, v in points.items()}).reindex(idx).ffill()
    dep = pd.Series({pd.Timestamp(d): v[1] for d, v in points.items()}).reindex(idx).ffill()
    rate = pd.Series({pd.Timestamp(d): r for d, r in (fx or {}).items()}).reindex(idx).ffill().bfill() \
        if fx else pd.Series(1.0, index=idx)
    # the base-currency deposits are the running sum of the amounts at the rate of the day they were paid in (a deposit
    # does not change its base-currency amount when the exchange rate moves afterwards)
    dep_base = (dep.diff().fillna(dep) * rate).cumsum()
    f = pd.DataFrame({"total_usd": value, "deposits_usd": dep, "stocks_usd": value, "cash_usd": 0.0,
                      "base_per_usd": rate, "total_base": value * rate, "deposits_base": dep_base}, index=idx)
    pos = positions if positions is not None else pd.DataFrame(index=idx)
    return History(base, first, last, f, pos, benchmark_symbol="BENCH", benchmark_available=True, benchmark_priced=True,
                   position_keys=keys or {})


def weekdays(first: date, n: int) -> list[date]:
    out, d = [], first
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def walk(returns: np.ndarray, first: date = MON, capital: float = 10_000.0) -> History:
    """A portfolio funded with ``capital`` on ``first`` whose value then moves by ``returns`` on the following weekdays."""
    days = weekdays(first, len(returns) + 1)
    value, pts = capital, {days[0]: (capital, capital)}
    for d, r in zip(days[1:], returns):
        value *= 1.0 + r
        pts[d] = (value, capital)
    return make_history(pts)


def rng_returns(n: int, seed: int = 7, mu: float = 0.0005, sigma: float = 0.01) -> np.ndarray:
    return np.random.default_rng(seed).normal(mu, sigma, n)


def bench_from(days: list[date], returns: np.ndarray, start_price: float = 100.0) -> list[tuple[date, float]]:
    """Benchmark closes on ``days`` (the first is ``start_price``, then moved by ``returns``)."""
    prices = [start_price]
    for r in returns:
        prices.append(prices[-1] * (1.0 + r))
    return list(zip(days, prices))


# ---------------------------------------------------------------------------------------------------------------
# observations: the time-weighted daily returns
# ---------------------------------------------------------------------------------------------------------------
def test_observations_remove_deposits_and_withdrawals_by_hand():
    d = lambda n: MON + timedelta(days=n)                                              # noqa: E731
    h = make_history({
        d(0): (1000.0, 1000.0),          # Mon  deposit 1000                         -> nothing moved: skipped
        d(1): (1010.0, 1000.0),          # Tue  +1 %                                  -> first observation, from 1000
        d(2): (1020.1, 1000.0),          # Wed  +1 %
        d(3): (1500.0, 1500.0),          # Thu  deposit 500: 1500 / (1020.1 + 500) - 1
        d(4): (1500.0, 1500.0),          # Fri  nothing moved (holiday)              -> skipped
        d(7): (1530.0, 1500.0),          # Mon  +2 % (Sat / Sun carry Friday forward)
        d(8): (1260.0, 1200.0),          # Tue  withdrawal 300: 1260 / (1530 - 300) - 1
    })
    obs = an.observations(h)
    assert [t.date() for t in obs.index] == [d(1), d(2), d(3), d(7), d(8)]
    assert obs["flow"].tolist() == [1000.0, 0.0, 500.0, 0.0, -300.0]
    expected = [1010.0 / 1000.0 - 1, 1020.1 / 1010.0 - 1, 1500.0 / (1020.1 + 500.0) - 1, 1530.0 / 1500.0 - 1,
                1260.0 / (1530.0 - 300.0) - 1]
    assert obs["ret"].tolist() == pytest.approx(expected, rel=1e-12)
    assert obs["value"].tolist() == [1010.0, 1020.1, 1500.0, 1530.0, 1260.0]


def test_a_deposit_on_a_weekend_rolls_into_the_next_trading_day():
    d = lambda n: MON + timedelta(days=n)                                              # noqa: E731
    h = make_history({
        d(0): (1000.0, 1000.0), d(1): (1000.0, 1000.0), d(2): (1010.0, 1000.0), d(3): (1020.0, 1000.0),
        d(4): (1030.0, 1000.0),          # Fri
        d(5): (1230.0, 1200.0),          # Sat  deposit 200 (no market on a Saturday)
        d(7): (1242.0, 1200.0),          # Mon  +1 % on the 1230 that were at work
    })
    obs = an.observations(h)
    assert obs.index[-1].date() == d(7) and obs["flow"].iloc[-1] == 200.0
    assert obs["ret"].iloc[-1] == pytest.approx(1242.0 / (1030.0 + 200.0) - 1, rel=1e-12)      # not (1242 / 1030) - 1


def test_days_without_a_value_and_with_no_capital_are_not_observations():
    d = lambda n: MON + timedelta(days=n)                                              # noqa: E731
    h = make_history({d(0): (0.0, 0.0), d(1): (0.0, 0.0), d(2): (500.0, 500.0), d(3): (505.0, 500.0)})
    h.frame.loc[pd.Timestamp(d(1)), "total_usd"] = float("nan")                         # a day nobody could value
    obs = an.observations(h)
    assert [t.date() for t in obs.index] == [d(3)]            # d(2) is the funding day (nothing moved), d(0)/d(1) no capital
    assert obs["flow"].iloc[0] == 500.0 and obs["ret"].iloc[0] == pytest.approx(0.01)


def test_the_valuation_date_is_an_observation_even_on_a_weekend():
    d = lambda n: MON + timedelta(days=n)                                              # noqa: E731
    h = make_history({d(0): (1000.0, 1000.0), d(1): (1010.0, 1000.0), d(5): (1020.0, 1000.0)})       # ends on a Saturday
    obs = an.observations(h)
    assert [t.date() for t in obs.index] == [d(1), d(5)]
    assert obs["ret"].iloc[-1] == pytest.approx(1020.0 / 1010.0 - 1)


def test_empty_history_has_no_observations():
    h = History("USD", None, None, pd.DataFrame(), pd.DataFrame())
    assert an.observations(h).empty


# ---------------------------------------------------------------------------------------------------------------
# the statistics against numpy
# ---------------------------------------------------------------------------------------------------------------
def independent(r: np.ndarray, rf: float = 0.0) -> dict:
    """The same metrics written again, differently (pandas, explicit loops), as the reference of the tests."""
    s = pd.Series(r)
    m = (1 + rf) ** (1 / 252) - 1
    wealth = (1 + s).cumprod()
    wealth0 = pd.concat([pd.Series([1.0]), wealth], ignore_index=True)
    dd = wealth0 / wealth0.cummax() - 1
    below = np.array([min(x - m, 0.0) for x in r])
    q = np.percentile(r, 5)
    return dict(
        cum=wealth.iloc[-1] - 1, vol=s.std() * math.sqrt(252), sharpe=(s.mean() - m) / s.std() * math.sqrt(252),
        downside=math.sqrt((below ** 2).sum() / len(r)) * math.sqrt(252),
        sortino=(s.mean() - m) / math.sqrt((below ** 2).sum() / len(r)) * math.sqrt(252),
        mdd=dd.min(), cur_dd=dd.iloc[-1], var=-q, cvar=-s[s <= q].mean(), best=s.max(), worst=s.min(),
        pos=(s > 0).mean(), mean=s.mean(), dd=dd)


def test_metrics_match_an_independent_calculation():
    r = rng_returns(300)
    h = walk(r)
    rep = an.risk_report(h, bare_md(), risk_free=0.03)
    ref = independent(r, 0.03)
    p = rep.portfolio
    assert rep.ok and rep.n_obs == 300 and p.n == 300
    assert p.cumulative == pytest.approx(ref["cum"], rel=1e-10)
    assert p.volatility == pytest.approx(ref["vol"], rel=1e-10)
    assert p.sharpe == pytest.approx(ref["sharpe"], rel=1e-10)
    assert p.downside_deviation == pytest.approx(ref["downside"], rel=1e-10)
    assert p.sortino == pytest.approx(ref["sortino"], rel=1e-10)
    assert p.max_drawdown == pytest.approx(ref["mdd"], rel=1e-10)
    assert p.current_drawdown == pytest.approx(ref["cur_dd"], abs=1e-12)
    assert p.var95 == pytest.approx(ref["var"], rel=1e-10) and p.cvar95 == pytest.approx(ref["cvar"], rel=1e-10)
    assert p.best_day == pytest.approx(ref["best"]) and p.worst_day == pytest.approx(ref["worst"])
    assert p.positive_share == pytest.approx(ref["pos"]) and p.mean_daily == pytest.approx(ref["mean"])
    years = (rep.end - rep.start).days / 365.0
    assert rep.years == pytest.approx(years)
    assert p.annualized == pytest.approx((1 + ref["cum"]) ** (1 / years) - 1, rel=1e-10)
    assert p.calmar == pytest.approx(p.annualized / abs(ref["mdd"]), rel=1e-10)


def test_the_risk_free_rate_only_moves_the_ratios():
    r = rng_returns(200, seed=3)
    h = walk(r)
    a, b = an.risk_report(h, bare_md(), risk_free=0.0), an.risk_report(h, bare_md(), risk_free=0.05)
    assert a.portfolio.volatility == b.portfolio.volatility and a.portfolio.max_drawdown == b.portfolio.max_drawdown
    assert b.portfolio.sharpe < a.portfolio.sharpe and b.portfolio.sortino < a.portfolio.sortino
    m = 1.05 ** (1 / 252) - 1
    assert a.portfolio.sharpe - b.portfolio.sharpe == pytest.approx(m / r.std(ddof=1) * math.sqrt(252), rel=1e-9)


def test_drawdown_dates_and_recovery_by_hand():
    # wealth: 1.0 -> 1.10 (peak, day 1) -> 1.045 -> 0.99275 (trough, day 3: -9.75 %) -> 1.11188 (day 4: above 1.10 again)
    returns = np.array([0.10, -0.05, -0.05, 0.12, 0.10])
    days = weekdays(MON, 6)
    st = an.series_stats(days[1:], returns, days[0], 5 / 365, ratios=False)
    assert st.max_drawdown == pytest.approx(1.10 * 0.95 * 0.95 / 1.10 - 1) == pytest.approx(-0.0975)
    assert (st.mdd_peak, st.mdd_trough, st.mdd_recovery) == (days[1], days[3], days[4])
    assert st.current_drawdown == 0.0 and st.cumulative == pytest.approx(1.1 * 0.95 * 0.95 * 1.12 * 1.10 - 1)


def test_a_fall_that_has_not_recovered_has_no_recovery_date_and_a_series_that_never_falls_has_no_fall():
    days = weekdays(MON, 5)
    falling = an.series_stats(days[1:], np.array([0.05, -0.10, 0.02, 0.01]), days[0], 0.1, ratios=False)
    assert falling.mdd_recovery is None and falling.mdd_peak == days[1] and falling.mdd_trough == days[2]
    assert falling.current_drawdown == pytest.approx(1.05 * 0.9 * 1.02 * 1.01 / 1.05 - 1)
    rising = an.series_stats(days[1:], np.array([0.01, 0.02, 0.0, 0.01]), days[0], 0.1, ratios=False)
    assert rising.max_drawdown == 0.0 and rising.mdd_peak is None and an.drawdown_period(rising) == "no fall"
    assert "not recovered yet" in an.drawdown_period(falling)


def test_the_drawdown_can_start_from_the_first_day():
    days = weekdays(MON, 4)
    st = an.series_stats(days[1:], np.array([-0.10, 0.05, 0.05]), days[0], 0.1, ratios=False)
    assert st.mdd_peak == days[0] and st.mdd_trough == days[1] and st.max_drawdown == pytest.approx(-0.10)
    assert st.mdd_recovery is None                                                  # 0.9 * 1.05 * 1.05 = 0.99225 < 1


def test_annualised_return_waits_for_three_months():
    r = rng_returns(40)
    rep = an.risk_report(walk(r), bare_md())                                        # 40 weekdays = 56 days < 0.25 years
    assert rep.portfolio.annualized is None and rep.portfolio.calmar is None
    assert any("three months" in n for n in rep.notes)
    assert rep.portfolio.volatility is not None


def test_a_flat_portfolio_has_no_sharpe_and_no_downside_ratio():
    st = an.series_stats(weekdays(MON, 31)[1:], np.zeros(30), MON, 0.5)
    assert st.volatility == 0.0 and st.sharpe is None and st.sortino is None and st.max_drawdown == 0.0


# ---------------------------------------------------------------------------------------------------------------
# the benchmark
# ---------------------------------------------------------------------------------------------------------------
def test_benchmark_statistics_match_numpy():
    rb = rng_returns(260, seed=11, mu=0.0004, sigma=0.009)
    noise = rng_returns(260, seed=12, mu=0.0001, sigma=0.004)
    rp = 0.8 * rb + noise
    h = walk(rp)
    days = weekdays(MON, 261)
    rep = an.risk_report(h, bare_md(bench_from(days, rb)), risk_free=0.02)
    rel, b = rep.relative, rep.benchmark
    assert rep.ok and rel is not None and b is not None
    x, y = rb[1:], rp[1:]                                                           # the first day has no benchmark return
    m = 1.02 ** (1 / 252) - 1
    beta = np.cov(y, x, ddof=1)[0, 1] / np.var(x, ddof=1)
    assert rel.n == 259 and rel.beta == pytest.approx(beta, rel=1e-10)
    assert rel.correlation == pytest.approx(np.corrcoef(y, x)[0, 1], rel=1e-10)
    assert rel.alpha == pytest.approx(((y - m).mean() - beta * (x - m).mean()) * 252, rel=1e-10)
    assert rel.tracking_error == pytest.approx((y - x).std(ddof=1) * math.sqrt(252), rel=1e-10)
    assert rel.information_ratio == pytest.approx((y - x).mean() / (y - x).std(ddof=1) * math.sqrt(252), rel=1e-10)
    assert rel.treynor == pytest.approx((y.mean() - m) * 252 / beta, rel=1e-10)
    ref = independent(x, 0.02)
    assert b.volatility == pytest.approx(ref["vol"], rel=1e-10) and b.sharpe == pytest.approx(ref["sharpe"], rel=1e-10)
    assert b.max_drawdown == pytest.approx(ref["mdd"], rel=1e-10) and b.cumulative == pytest.approx(ref["cum"], rel=1e-10)
    assert 0.6 < rel.beta < 1.0                                                      # built with a beta of 0.8


def test_the_last_day_of_the_benchmark_uses_the_current_price():
    rb = rng_returns(60, seed=5, sigma=0.008)
    days = weekdays(MON, 61)
    bench = bench_from(days, rb)
    last = bench[-1][1]
    over = an.risk_report(walk(rng_returns(60, seed=6)), bare_md(bench, bench_over=last * 1.01))
    plain = an.risk_report(walk(rng_returns(60, seed=6)), bare_md(bench))
    assert over.frame["bench_ret"].iloc[-1] == pytest.approx(last * 1.01 / bench[-2][1] - 1)
    assert plain.frame["bench_ret"].iloc[-1] == pytest.approx(last / bench[-2][1] - 1)


def test_without_benchmark_prices_the_benchmark_columns_are_empty_and_a_note_says_so():
    rep = an.risk_report(walk(rng_returns(80)), bare_md())
    assert rep.ok and rep.benchmark is None and rep.relative is None
    assert any("No benchmark prices (BENCH)" in n for n in rep.notes)
    rows = rep.rows()
    assert rows and all(r.benchmark is None for r in rows) and not any(r.group.startswith("Against") for r in rows)


def test_a_benchmark_that_starts_late_is_only_compared_from_its_first_price():
    days = weekdays(MON, 101)
    rb = rng_returns(60, seed=21, sigma=0.008)
    bench = bench_from(days[40:], rb)                                                # prices from observation 41 on
    rep = an.risk_report(walk(rng_returns(100, seed=22)), bare_md(bench))
    # 61 benchmark prices (from the 40th observation on) are 60 returns, the first of them on the 41st observation
    assert rep.benchmark is not None and rep.benchmark.n == 60 and rep.relative.n == 60
    assert np.isnan(rep.frame["bench_ret"].iloc[:40]).all() and np.isfinite(rep.frame["bench_ret"].iloc[40:]).all()
    assert rep.benchmark.cumulative == pytest.approx(np.prod(1 + rb) - 1, rel=1e-12)


# ---------------------------------------------------------------------------------------------------------------
# windows, units, gates
# ---------------------------------------------------------------------------------------------------------------
def test_the_period_restricts_the_window():
    r = rng_returns(300, seed=2)
    h = walk(r)
    days = weekdays(MON, 301)
    since = days[200]
    rep = an.risk_report(h, bare_md(), since=since)
    tail = r[199:]                                                                    # observations dated >= days[200]
    assert rep.start == since and rep.end == days[-1] and rep.n_obs == len(tail) == 101
    assert rep.portfolio.volatility == pytest.approx(pd.Series(tail).std() * math.sqrt(252), rel=1e-10)
    assert rep.portfolio.cumulative == pytest.approx(np.prod(1 + tail) - 1, rel=1e-10)
    assert rep.years == pytest.approx((days[-1] - since).days / 365.0)
    full = an.risk_report(h, bare_md())
    assert full.n_obs == 300 and full.start == days[0]


def test_a_window_with_few_days_gives_return_and_drawdown_only():
    r = rng_returns(100, seed=4)
    rep = an.risk_report(walk(r), bare_md(), since=weekdays(MON, 101)[90])
    assert rep.status == an.FEW_DAYS and rep.n_obs == 11 and "at least 20" in rep.message
    assert rep.portfolio.cumulative is not None and rep.portfolio.max_drawdown is not None
    assert rep.portfolio.volatility is None and rep.portfolio.sharpe is None and rep.frame.empty
    labels = [row.label for row in rep.rows()]
    assert "Time-weighted return" in labels and "Maximum drawdown" in labels and "Sharpe ratio" not in labels


def test_the_base_currency_view_includes_the_currency_effect():
    days = weekdays(MON, 81)
    usd = {d: (10_000.0 * 1.001 ** i, 10_000.0) for i, d in enumerate(days)}          # +0.1 % a day in USD
    huf = {d: 300.0 * 1.002 ** i for i, d in enumerate(days)}                          # HUF per USD +0.2 % a day
    h = make_history(usd, base="HUF", fx=huf)
    u, b = an.risk_report(h, bare_md(), "usd"), an.risk_report(h, bare_md(), "base")
    assert (u.unit, u.currency, b.unit, b.currency) == ("usd", "USD", "base", "HUF")
    assert u.portfolio.cumulative == pytest.approx(1.001 ** 80 - 1, rel=1e-9)
    assert b.portfolio.cumulative == pytest.approx((1.001 * 1.002) ** 80 - 1, rel=1e-9)
    assert u.portfolio.volatility == pytest.approx(0.0, abs=1e-12)                    # a constant drift has no volatility
    usd_only = an.risk_report(make_history(usd), bare_md(), "base")
    assert usd_only.unit == "usd" and usd_only.currency == "USD"                      # no separate base columns for a USD portfolio


def test_the_benchmark_is_converted_like_the_portfolio_in_the_base_currency():
    days = weekdays(MON, 81)
    usd = {d: (10_000.0 * (1 + 0.0003 * i), 10_000.0) for i, d in enumerate(days)}
    huf = {d: 300.0 * (1 + 0.001 * i) for i, d in enumerate(days)}
    bench = [(d, 100.0 * (1 + 0.0005 * i)) for i, d in enumerate(days)]
    rep = an.risk_report(make_history(usd, base="HUF", fx=huf), bare_md(bench), "base")
    i = 40
    expected = bench[i][1] * huf[days[i]] / (bench[i - 1][1] * huf[days[i - 1]]) - 1
    assert rep.frame["bench_ret"].iloc[i - 1] == pytest.approx(expected, rel=1e-12)       # obs 0 is day 1


def test_no_history_and_no_prices_are_reported_not_computed():
    none = an.risk_report(None, bare_md())
    assert none.status == an.NO_HISTORY and "no history" in none.message.lower() and none.rows() == []
    empty = an.risk_report(History("USD", None, None, pd.DataFrame(), pd.DataFrame()), bare_md())
    assert empty.status == an.NO_HISTORY


def _stocks_history(price_points: list[date], n: int = 90, source: str = SRC_YAHOO):
    r = rng_returns(n, seed=8)
    h = walk(r)
    days = weekdays(MON, n + 1)
    pos = pd.DataFrame({"AAPL": 1000.0}, index=h.frame.index)
    ser = PriceSeries(list(price_points), [100.0] * len(price_points), [source] * len(price_points), "AAPL")
    return replace_positions(h, pos, {"AAPL": "AAPL|USD"}), bare_md(price_series={"AAPL|USD": ser}), days


def replace_positions(h: History, pos: pd.DataFrame, keys: dict) -> History:
    return History(h.base_currency, h.start, h.end, h.frame, pos, benchmark_symbol=h.benchmark_symbol,
                   benchmark_available=True, benchmark_priced=True, position_keys=keys)


def test_statement_only_prices_give_no_metrics():
    h, md, days = _stocks_history([days0 for days0 in weekdays(MON, 1)], source=SRC_STATEMENT)    # one price, long ago
    rep = an.risk_report(h, md)
    assert rep.status == an.NO_PRICES and rep.price_coverage < 0.1 and "Daily prices are missing" in rep.message
    assert rep.rows() == [] and rep.frame.empty


def test_a_partly_priced_portfolio_gets_a_warning_and_a_well_priced_one_does_not():
    n = 90
    days = weekdays(MON, n + 1)
    h, md, _ = _stocks_history(days[: int(n * 0.7)], n)                              # quotes for the first 70 % of the days
    rep = an.risk_report(h, md)
    assert rep.ok and 0.5 <= rep.price_coverage < 0.9 and any("carried-forward prices" in x for x in rep.notes)
    h2, md2, _ = _stocks_history(days, n)
    full = an.risk_report(h2, md2)
    assert full.ok and full.price_coverage == pytest.approx(1.0) and not any("carried-forward" in x for x in full.notes)
    h3, md3, _ = _stocks_history(days[: int(n * 0.3)], n)                            # a 30 % covered portfolio
    assert an.risk_report(h3, md3).status == an.NO_PRICES


def test_the_price_coverage_counts_a_quote_of_the_last_five_days_as_current():
    days = weekdays(MON, 31)
    h = walk(rng_returns(30, seed=1))
    pos = pd.DataFrame({"AAPL": 500.0}, index=h.frame.index)
    ser = PriceSeries([days[0]], [100.0], [SRC_YAHOO], "AAPL")
    md = bare_md(price_series={"AAPL|USD": ser})
    h = replace_positions(h, pos, {"AAPL": "AAPL|USD"})
    obs_days = an.observations(h).index
    cov = an.price_coverage(h, md, obs_days)
    fresh = sum((t.date() - days[0]).days <= 5 for t in obs_days)
    assert cov == pytest.approx(fresh / len(obs_days)) and 0 < cov < 1
    assert an.price_coverage(h, bare_md(), obs_days) == 0.0                           # a stock with no price series at all


def test_few_observations_are_marked_indicative():
    rep = an.risk_report(walk(rng_returns(35)), bare_md())
    assert rep.ok and any("indicative" in n for n in rep.notes)
    assert not any("indicative" in n for n in an.risk_report(walk(rng_returns(120)), bare_md()).notes)


# ---------------------------------------------------------------------------------------------------------------
# the frame behind the charts, the tables and the helpers
# ---------------------------------------------------------------------------------------------------------------
def test_frame_columns_are_the_running_index_peak_drawdown_and_rolling_volatility():
    r = rng_returns(120, seed=9)
    rb = rng_returns(120, seed=10, sigma=0.008)
    rep = an.risk_report(walk(r), bare_md(bench_from(weekdays(MON, 121), rb)))
    f = rep.frame
    wealth = np.cumprod(1 + r)
    assert f["index"].to_numpy() == pytest.approx(wealth, rel=1e-12)
    assert f["peak"].to_numpy() == pytest.approx(np.maximum.accumulate(np.maximum(wealth, 1.0)), rel=1e-12)
    assert f["drawdown"].to_numpy() == pytest.approx(f["index"] / f["peak"] - 1, abs=1e-15)
    assert f["rolling_vol"].iloc[:29].isna().all() and f["rolling_vol"].iloc[29] == pytest.approx(
        pd.Series(r[:30]).std() * math.sqrt(252), rel=1e-10)
    assert f["rolling_vol"].iloc[-1] == pytest.approx(pd.Series(r[-30:]).std() * math.sqrt(252), rel=1e-10)
    assert np.isnan(f["bench_index"].iloc[0]) and f["bench_drawdown"].iloc[1:].le(1e-15).all()
    assert f["bench_index"].iloc[-1] == pytest.approx(np.prod(1 + rb[1:]), rel=1e-12)


def test_metric_rows_and_tables():
    rb = rng_returns(150, seed=31, sigma=0.008)
    rep = an.risk_report(walk(0.9 * rb + rng_returns(150, seed=32, sigma=0.003)), bare_md(bench_from(weekdays(MON, 151), rb)),
                         risk_free=0.01)
    rows = rep.rows()
    groups = list(dict.fromkeys(r.group for r in rows))
    assert groups == ["Return (time-weighted)", "Risk", "Risk-adjusted return", "Against the benchmark (BENCH)"]
    labels = {r.label for r in rows}
    assert {"Annualised volatility", "Maximum drawdown", "Sharpe ratio", "Sortino ratio", "Calmar ratio", "Beta",
            "Alpha (Jensen, annualised)", "Tracking error (annualised)", "Information ratio", "Treynor ratio",
            "Daily Value at Risk (95 %)", "Daily expected shortfall (95 %)"} <= labels
    shown = T.risk_table(rep)
    assert list(shown.columns) == ["Group", "Metric", "Portfolio", "Benchmark (BENCH)", "What it means"]
    vol = shown.loc[shown["Metric"] == "Annualised volatility", "Portfolio"].iloc[0]
    assert vol == f"{rep.portfolio.volatility * 100:.2f}%"
    values = T.risk_values_table(rep)
    assert values["Currency"].eq("USD").all() and values["Risk-free rate"].eq(0.01).all()
    sharpe = values.loc[values["Metric"] == "Sharpe ratio", "Portfolio"].iloc[0]
    assert sharpe == pytest.approx(rep.portfolio.sharpe)
    daily = T.risk_daily_table(rep)
    assert len(daily) == 150 and daily.columns[:3].tolist() == ["Date", "Currency", "Value"]
    assert daily["Date"].iloc[0] == (MON + timedelta(days=1)).isoformat()
    dd = T.risk_chart_frame(rep, "drawdown")
    assert list(dd.columns) == ["Portfolio", "BENCH"] and (dd.dropna() <= 1e-12).all().all()
    vol_frame = T.risk_chart_frame(rep, "rolling_vol")
    assert vol_frame["Portfolio"].iloc[29] == pytest.approx(rep.frame["rolling_vol"].iloc[29] * 100)
    with pytest.raises(ValueError):
        T.risk_chart_frame(rep, "nope")


def test_the_risk_free_rate_setting():
    assert an.risk_free_rate({"risk_free": {"USD": 4.5, "HUF": 6.25}}, "USD") == pytest.approx(0.045)
    assert an.risk_free_rate({"risk_free": {"HUF": "6.25"}}, "HUF") == pytest.approx(0.0625)
    for bad in ({}, None, {"risk_free": None}, {"risk_free": {"USD": "x"}}, {"risk_free": {"USD": 250}},
                {"risk_free": {"USD": float("nan")}}, {"risk_free": {"EUR": 3}}):
        assert an.risk_free_rate(bad, "USD") == 0.0
    assert an.risk_free_rate({"risk_free": {"USD": -0.5}}, "USD") == pytest.approx(-0.005)


def test_drawdown_curve_starts_at_one():
    wealth, peak, dd = an.drawdown_curve(np.array([0.1, -0.2]))
    assert wealth.tolist() == pytest.approx([1.0, 1.1, 0.88]) and peak.tolist() == pytest.approx([1.0, 1.1, 1.1])
    assert dd.tolist() == pytest.approx([0.0, 0.0, 0.88 / 1.1 - 1])
