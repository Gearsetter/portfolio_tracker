"""Risk and risk-adjusted-return metrics of the portfolio, computed from its day-by-day history.

The dashboard's CAGR and return answer *"what did my deposits earn?"* - they depend on **when** money was paid in.
Volatility, drawdown and the Sharpe family need a return series that does **not** depend on the deposits, so they are
computed from the **time-weighted daily returns** of the history (:mod:`ibkr_tracker.history`): deposits and
withdrawals are removed from the day they happen, a gain or a loss is measured against the capital that was at work.

Observation days
    Monday-Friday days of the history plus the valuation date.  A day on which nothing moved (market holiday, a price
    that was carried forward, a day with only a deposit) is not an observation - its flow is added to the next one.
    The returns are therefore *trading-day* returns and are annualised with :data:`TRADING_DAYS` = 252.

Daily return of an observation day ``t`` (``p`` = the previous observation)::

    r_t = V_t / (V_p + F_t) - 1          F_t = deposits - withdrawals since p, assumed to be at work for the whole day

(the first observation starts from ``V_p = 0``).  The wealth index is ``W_t = prod(1 + r)``, ``W = 1`` before the first
observation; the cumulative time-weighted return is ``W - 1``.

Metrics (all in the chosen currency, the benchmark ETF is converted the same way)
    * annualised volatility = ``stdev(r) * sqrt(252)`` (sample standard deviation);
    * downside deviation = ``sqrt(mean(min(r - m, 0)^2)) * sqrt(252)`` with ``m`` the daily risk-free rate (the Sortino
      target) and the mean taken over *all* observations;
    * maximum drawdown = the largest fall of ``W`` from a previous peak (the peak, the trough and the recovery date are
      reported), current drawdown = today's distance from the peak;
    * Sharpe = ``(mean(r) - m) / stdev(r) * sqrt(252)``;  Sortino = ``(mean(r) - m) / downside deviation * sqrt(252)``;
      Calmar = annualised return / |maximum drawdown|;
    * annualised return = ``(1 + cumulative)^(days_per_year / calendar days) - 1`` - left out while the window is younger
      than :data:`~ibkr_tracker.history.MIN_CAGR_YEARS` (annualising a few weeks says nothing);
    * 1-day historical Value at Risk / expected shortfall at 95 %: the 5th percentile of ``r`` and the mean of the days
      beyond it, reported as a positive loss;
    * against the benchmark (the same days): beta = ``cov(r, r_b) / var(r_b)``, correlation, Jensen's alpha =
      ``(mean(r - m) - beta * mean(r_b - m)) * 252``, tracking error = ``stdev(r - r_b) * sqrt(252)``, information ratio =
      ``mean(r - r_b) * 252 / tracking error``, Treynor = ``(mean(r) - m) * 252 / beta``.

What is *not* computed (and why)
    * **No daily prices** (only the closes printed in the statements): the curve between two statements is a straight
      line of carried-forward prices - volatility and drawdown of it would be fiction.  The report then says so.
    * Fewer than :data:`MIN_OBS` observation days: too few for a standard deviation worth reading.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from .history import MIN_CAGR_YEARS, History
from .market.data import MarketData

TRADING_DAYS = 252                  # observation days per year (annualisation of volatility, Sharpe, ...)
MIN_OBS = 20                        # fewer observation days -> no volatility / ratios
INDICATIVE_OBS = 60                 # fewer than this: the figures are shown with a warning
QUOTE_AGE_DAYS = 5                  # a price this old (weekend + a holiday) still counts as "a price of that day"
MIN_PRICE_COVERAGE = 0.5            # below: no metrics (the portfolio is mostly valued with carried-forward prices)
OK_PRICE_COVERAGE = 0.9             # below: metrics with a warning
ROLLING_WINDOW = 30                 # observation days of the rolling volatility
VAR_LEVEL = 0.95
EPS = 1e-9

# status of a report
OK, NO_HISTORY, NO_PRICES, FEW_DAYS = "ok", "no_history", "no_prices", "few_days"

# kinds of values (how a table shows them)
PCT, RATIO, NUMBER, TEXT = "pct", "ratio", "number", "text"


# ---------------------------------------------------------------------------------------------------------------
# result containers
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class Stats:
    """The metrics of one return series (the portfolio, or the benchmark on the same days)."""
    n: int = 0                                   # daily returns
    cumulative: Optional[float] = None           # time-weighted return over the window (0.12 = +12 %)
    annualized: Optional[float] = None
    volatility: Optional[float] = None           # annualised
    downside_deviation: Optional[float] = None   # annualised
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    calmar: Optional[float] = None
    max_drawdown: Optional[float] = None         # <= 0
    mdd_peak: Optional[date] = None
    mdd_trough: Optional[date] = None
    mdd_recovery: Optional[date] = None          # None while max_drawdown < 0: not recovered yet
    current_drawdown: Optional[float] = None
    best_day: Optional[float] = None
    worst_day: Optional[float] = None
    positive_share: Optional[float] = None       # share of the days with a gain
    var95: Optional[float] = None                # 1-day historical VaR, a positive loss
    cvar95: Optional[float] = None               # expected shortfall
    mean_daily: Optional[float] = None


@dataclass
class Relative:
    """The portfolio against the benchmark (on the days both have a return)."""
    n: int = 0
    beta: Optional[float] = None
    correlation: Optional[float] = None
    alpha: Optional[float] = None                # Jensen, annualised
    tracking_error: Optional[float] = None       # annualised
    information_ratio: Optional[float] = None
    treynor: Optional[float] = None


@dataclass
class MetricRow:
    """One line of the metrics table."""
    group: str
    key: str
    label: str
    portfolio: object = None                     # number / text / None
    benchmark: object = None
    kind: str = PCT                              # PCT | RATIO | NUMBER | TEXT
    help: str = ""


@dataclass
class RiskReport:
    unit: str                                    # "usd" | "base"
    currency: str
    risk_free: float                             # annual, as a fraction (0.04 = 4 %)
    status: str = OK
    message: str = ""
    start: Optional[date] = None                 # first day of the window (the first observation lies on / after it)
    end: Optional[date] = None
    years: Optional[float] = None
    n_obs: int = 0
    price_coverage: Optional[float] = None       # share of the stock value priced with a recent quote
    portfolio: Stats = field(default_factory=Stats)
    benchmark: Optional[Stats] = None
    benchmark_symbol: str = ""
    relative: Optional[Relative] = None
    frame: pd.DataFrame = field(default_factory=pd.DataFrame)     # one row per observation day (see _frame)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == OK

    def rows(self) -> list[MetricRow]:
        return metric_rows(self)


# ---------------------------------------------------------------------------------------------------------------
# observations
# ---------------------------------------------------------------------------------------------------------------
def unit_of(h: History, unit: str) -> str:
    """``"base"`` only when the base currency differs from USD (a USD portfolio has no separate base columns)."""
    return "base" if (unit == "base" and h.base_currency != "USD") else "usd"


def observations(h: History, unit: str = "usd") -> pd.DataFrame:
    """The observation days of the whole history: ``value``, ``flow`` (money paid in less money taken out since the
    previous observation) and the time-weighted daily return ``ret``.  Indexed by date (see the module docstring)."""
    if h.empty:
        return pd.DataFrame(columns=["value", "flow", "ret"])
    f = h.frame
    u = unit_of(h, unit)
    value = f[f"total_{u}"].to_numpy(dtype=float)
    deposits = f[f"deposits_{u}"].to_numpy(dtype=float)
    idx = f.index
    weekday = np.asarray(idx.day_of_week) < 5
    weekday[-1] = True                                      # the valuation date is always a candidate
    days, vals, flows, rets = [], [], [], []
    prev_v = prev_dep = 0.0                                 # state at the previous *observation*
    for i in np.flatnonzero(weekday):
        v, dep = value[i], deposits[i]
        if not (math.isfinite(v) and math.isfinite(dep)):
            continue
        flow = dep - prev_dep
        denom = prev_v + flow
        if denom <= EPS:
            continue                                        # no capital at work yet
        if abs(v - denom) <= 1e-9 * max(1.0, abs(denom)):
            continue                                        # nothing moved: holiday / carried-forward prices
        days.append(idx[i])
        vals.append(v)
        flows.append(flow)
        rets.append(v / denom - 1.0)
        prev_v, prev_dep = v, dep
    return pd.DataFrame({"value": vals, "flow": flows, "ret": rets},
                        index=pd.DatetimeIndex(days, name="Date"))


def benchmark_prices(h: History, md: MarketData, days: pd.DatetimeIndex, unit: str = "usd") -> pd.Series:
    """The benchmark's price on every observation day in the unit's currency (``NaN`` before its first price).  The
    last day uses the current price, like the history's own benchmark replay."""
    bench = md.benchmark
    out = np.full(len(days), np.nan)
    if not bench.dates:
        return pd.Series(out, index=days)
    u = unit_of(h, unit)
    last_day = h.frame.index[-1] if not h.empty else None
    first = bench.dates[0]
    for i, ts in enumerate(days):
        d = ts.date()
        if d < first:
            continue
        price = md.benchmark_price() if ts == last_day else bench.close(d)
        if not price:
            continue
        if u == "base":
            rate = h.frame["base_per_usd"].get(ts, np.nan)
            price = price * rate
        out[i] = price
    return pd.Series(out, index=days)


def price_coverage(h: History, md: MarketData, days: pd.DatetimeIndex) -> float:
    """Share of the stock value (summed over ``days``) that was priced with a quote at most :data:`QUOTE_AGE_DAYS`
    old.  A portfolio without stock value counts as fully covered (there is nothing to price)."""
    if h.empty or not len(days) or h.positions.empty:
        return 1.0
    ordinals = np.array([ts.toordinal() for ts in days])
    total = fresh = 0.0
    for label in h.positions.columns:
        values = np.abs(h.positions[label].reindex(days).to_numpy(dtype=float))
        values = np.nan_to_num(values)
        ser = md.price_series.get(h.position_keys.get(label, ""))
        if ser is None or not len(ser):
            total += float(values.sum())
            continue
        pts = np.array([d.toordinal() for d in ser.dates])
        i = np.searchsorted(pts, ordinals, side="right") - 1
        age = np.where(i >= 0, ordinals - pts[np.maximum(i, 0)], np.inf)
        total += float(values.sum())
        fresh += float(values[age <= QUOTE_AGE_DAYS].sum())
    return fresh / total if total > EPS else 1.0


# ---------------------------------------------------------------------------------------------------------------
# statistics of a return series
# ---------------------------------------------------------------------------------------------------------------
def risk_free_rate(settings: Optional[dict], currency: str) -> float:
    """The annual risk-free rate the user entered for ``currency`` (the settings keep percent) as a fraction; 0 when
    nothing valid was entered.  Only -50 % ... 100 % is accepted - anything else is a typing error."""
    raw = ((settings or {}).get("risk_free") or {}).get(currency)
    try:
        pct = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return pct / 100.0 if math.isfinite(pct) and -50.0 <= pct <= 100.0 else 0.0


def _rf_daily(risk_free: float) -> float:
    return (1.0 + risk_free) ** (1.0 / TRADING_DAYS) - 1.0


def drawdown_curve(returns: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``(wealth, peak, drawdown)`` arrays of length ``len(returns) + 1`` - position 0 is the start (wealth 1)."""
    wealth = np.concatenate([[1.0], np.cumprod(1.0 + returns)])
    peak = np.maximum.accumulate(wealth)
    return wealth, peak, wealth / peak - 1.0


def series_stats(dates: list[date], returns: np.ndarray, start: Optional[date], years: Optional[float],
                 risk_free: float = 0.0, *, ratios: bool = True) -> Stats:
    """Metrics of one return series.  ``dates`` are the observation days of ``returns``; ``start`` is the day the first
    return starts from (the date of the wealth index 1.0); ``years`` the length of the window for annualising.  With
    ``ratios=False`` (fewer than :data:`MIN_OBS` returns) only the return and the drawdown are filled."""
    r = np.asarray(returns, dtype=float)
    n = len(r)
    st = Stats(n=n)
    if n == 0:
        return st
    wealth, peak, dd = drawdown_curve(r)
    st.cumulative = float(wealth[-1] - 1.0)
    if years is not None and years >= MIN_CAGR_YEARS and wealth[-1] > 0:
        st.annualized = float(wealth[-1] ** (1.0 / years) - 1.0)
    # drawdown: the fall, its peak / trough dates and the recovery
    k = int(np.argmin(dd))
    st.max_drawdown = float(dd[k])
    st.current_drawdown = float(dd[-1])
    all_days = [start] + list(dates)
    if dd[k] < 0:
        peak_idx = int(np.flatnonzero(wealth[: k + 1] == peak[k])[-1])
        st.mdd_peak, st.mdd_trough = all_days[peak_idx], all_days[k]
        later = np.flatnonzero(wealth[k + 1:] >= peak[k])
        st.mdd_recovery = all_days[k + 1 + int(later[0])] if len(later) else None
    st.best_day, st.worst_day = float(r.max()), float(r.min())
    st.positive_share = float((r > 0).mean())
    st.mean_daily = float(r.mean())
    if not ratios:
        return st
    m = _rf_daily(risk_free)
    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    root = math.sqrt(TRADING_DAYS)
    st.volatility = sd * root
    downside = math.sqrt(float(np.mean(np.minimum(r - m, 0.0) ** 2)))
    st.downside_deviation = downside * root
    if sd > 0:
        st.sharpe = (float(r.mean()) - m) / sd * root
    if downside > 0:
        st.sortino = (float(r.mean()) - m) / downside * root
    if st.annualized is not None and st.max_drawdown and st.max_drawdown < 0:
        st.calmar = st.annualized / abs(st.max_drawdown)
    q = float(np.quantile(r, 1.0 - VAR_LEVEL))
    st.var95 = max(-q, 0.0)
    tail = r[r <= q]
    st.cvar95 = max(-float(tail.mean()), 0.0) if len(tail) else None
    return st


def relative_stats(rp: np.ndarray, rb: np.ndarray, risk_free: float = 0.0) -> Relative:
    """Beta, correlation, alpha, tracking error, information ratio and Treynor of ``rp`` against ``rb``."""
    rp, rb = np.asarray(rp, dtype=float), np.asarray(rb, dtype=float)
    out = Relative(n=len(rp))
    if len(rp) < MIN_OBS:
        return out
    m = _rf_daily(risk_free)
    var_b = float(rb.var(ddof=1))
    if var_b > 0:
        out.beta = float(np.cov(rp, rb, ddof=1)[0, 1] / var_b)
        out.alpha = float(((rp - m).mean() - out.beta * (rb - m).mean()) * TRADING_DAYS)
        if abs(out.beta) > 1e-12:
            out.treynor = float((rp.mean() - m) * TRADING_DAYS / out.beta)
    if float(rp.std(ddof=1)) > 0 and var_b > 0:
        out.correlation = float(np.corrcoef(rp, rb)[0, 1])
    ex = rp - rb
    te = float(ex.std(ddof=1))
    out.tracking_error = te * math.sqrt(TRADING_DAYS)
    if te > 0:
        out.information_ratio = float(ex.mean() / te * math.sqrt(TRADING_DAYS))
    return out


# ---------------------------------------------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------------------------------------------
def _simple_returns(prices: np.ndarray) -> np.ndarray:
    """``p[t] / p[t-1] - 1`` with ``NaN`` for the first element and wherever a price is missing."""
    out = np.full(len(prices), np.nan)
    if len(prices) > 1:
        with np.errstate(divide="ignore", invalid="ignore"):
            out[1:] = prices[1:] / prices[:-1] - 1.0
    return out


def risk_report(h: Optional[History], md: MarketData, unit: str = "usd", risk_free: float = 0.0,
                since: Optional[date] = None, days_per_year: float = 365.0) -> RiskReport:
    """Risk and risk-adjusted return of the portfolio in ``unit`` (``"usd"`` or ``"base"``), from ``since`` (``None`` =
    the whole lifespan) to the valuation date.  ``risk_free`` is the annual risk-free rate as a fraction."""
    rep = RiskReport(unit="usd", currency="USD", risk_free=risk_free)
    if h is None or h.empty:
        rep.status, rep.message = NO_HISTORY, "There is no history to analyse - the statements contain no transactions."
        return rep
    rep.unit = unit_of(h, unit)
    rep.currency = h.base_currency if rep.unit == "base" else "USD"
    rep.benchmark_symbol = h.benchmark_symbol
    rep.end = h.end
    rep.start = max(h.start, since) if (since and h.start) else h.start
    rep.years = ((rep.end - rep.start).days / days_per_year) if rep.start and rep.end else None

    full = observations(h, rep.unit)
    bench_px = benchmark_prices(h, md, full.index, rep.unit)
    full["bench_price"] = bench_px.to_numpy()
    full["bench_ret"] = _simple_returns(full["bench_price"].to_numpy(dtype=float))      # between observation days
    obs = full if since is None else full.loc[full.index >= pd.Timestamp(since)]
    rep.n_obs = len(obs)
    if rep.n_obs:
        rep.price_coverage = price_coverage(h, md, obs.index)
    if rep.n_obs and rep.price_coverage < MIN_PRICE_COVERAGE:
        rep.status = NO_PRICES
        rep.message = (f"Daily prices are missing: only {rep.price_coverage:.0%} of the stock value has a price from "
                       "the last few days, the rest is carried forward or valued at the purchase price - volatility "
                       "and drawdown of such a curve would say nothing. Press 'Refresh Prices' (or add a manual price "
                       "history under Market data) and look at the price coverage there.")
        return rep
    dates = [t.date() for t in obs.index]
    if rep.n_obs < MIN_OBS:
        rep.status = FEW_DAYS
        rep.message = (f"Only {rep.n_obs} trading day(s) with a price change in this window - at least {MIN_OBS} are "
                       "needed for volatility and the ratios. Choose a longer period.")
        if rep.n_obs:                                           # the return and the drawdown are still worth showing
            rep.portfolio = series_stats(dates, obs["ret"].to_numpy(), rep.start, rep.years, risk_free, ratios=False)
        return rep

    rep.frame = _frame(obs)
    rep.portfolio = series_stats(dates, obs["ret"].to_numpy(), rep.start, rep.years, risk_free)
    if rep.price_coverage < OK_PRICE_COVERAGE:
        rep.notes.append(f"Only {rep.price_coverage:.0%} of the stock value is priced with a recent quote - part of "
                         "the portfolio is valued with carried-forward prices, so volatility and drawdown are "
                         "understated. See Market data > Price coverage.")
    if rep.n_obs < INDICATIVE_OBS:
        rep.notes.append(f"Only {rep.n_obs} trading days: the figures are indicative.")
    if rep.years is not None and rep.years < MIN_CAGR_YEARS:
        rep.notes.append("Annualised return and Calmar appear once the window is three months long.")

    rb = obs["bench_ret"].to_numpy(dtype=float)
    ok = np.isfinite(rb)
    if ok.sum() >= 2:
        first = int(np.flatnonzero(ok)[0])                       # the benchmark's index starts one observation earlier
        bstart = dates[first - 1] if first > 0 else rep.start
        rep.benchmark = series_stats([d for d, good in zip(dates, ok) if good], rb[ok], bstart, rep.years, risk_free,
                                     ratios=int(ok.sum()) >= MIN_OBS)
        if int(ok.sum()) >= MIN_OBS:
            rep.relative = relative_stats(obs["ret"].to_numpy()[ok], rb[ok], risk_free)
    elif h.benchmark_symbol:
        rep.notes.append(f"No benchmark prices ({h.benchmark_symbol}) in this window - the benchmark columns are empty.")
    return rep


def _frame(obs: pd.DataFrame) -> pd.DataFrame:
    """The observation table the charts and the Excel *Risk* sheet use: value, flow, return, wealth index, peak,
    drawdown, rolling volatility - and the benchmark's price, return, wealth index and drawdown."""
    out = obs.copy()
    wealth, peak, dd = drawdown_curve(out["ret"].to_numpy(dtype=float))
    out["index"], out["peak"], out["drawdown"] = wealth[1:], peak[1:], dd[1:]
    out["rolling_vol"] = out["ret"].rolling(ROLLING_WINDOW).std() * math.sqrt(TRADING_DAYS)
    rb = out["bench_ret"].to_numpy(dtype=float)
    valid = np.isfinite(rb)
    if valid.any():
        bw = np.cumprod(1.0 + np.where(valid, rb, 0.0))
        bpeak = np.maximum.accumulate(np.concatenate([[1.0], bw]))[1:]
        started = np.cumsum(valid) > 0
        out["bench_index"] = np.where(started, bw, np.nan)
        out["bench_drawdown"] = np.where(started, bw / bpeak - 1.0, np.nan)
    else:
        out["bench_index"] = np.nan
        out["bench_drawdown"] = np.nan
    return out


# ---------------------------------------------------------------------------------------------------------------
# the metrics table
# ---------------------------------------------------------------------------------------------------------------
DRAWDOWN_PERIOD_LABEL = "Maximum drawdown - period"


def drawdown_period(st: Stats) -> str:
    if st.mdd_peak is None or st.mdd_trough is None:
        return "no fall" if st.max_drawdown == 0 else ""
    rec = f", recovered {st.mdd_recovery}" if st.mdd_recovery else ", not recovered yet"
    return f"{st.mdd_peak} to {st.mdd_trough}{rec}"


def metric_rows(rep: RiskReport) -> list[MetricRow]:
    """The metrics table: portfolio and benchmark values per metric, with a short explanation."""
    if rep.status not in (OK, FEW_DAYS) or rep.portfolio.n == 0:
        return []
    p, b, rel = rep.portfolio, rep.benchmark, rep.relative
    bv = lambda name: getattr(b, name) if b is not None else None                        # noqa: E731
    R, K, X = "Return (time-weighted)", "Risk", "Risk-adjusted return"
    rows = [
        MetricRow(R, "cumulative", "Time-weighted return", p.cumulative, bv("cumulative"), PCT,
                  "Compounded daily returns with deposits and withdrawals removed - what the money at work earned."),
        MetricRow(R, "annualized", "Annualised return", p.annualized, bv("annualized"), PCT,
                  "The time-weighted return scaled to one year; shown once the window is three months long."),
    ]
    if rep.status == OK:
        rows += [
            MetricRow(K, "volatility", "Annualised volatility", p.volatility, bv("volatility"), PCT,
                      "Standard deviation of the daily returns x sqrt(252): how much the value swings."),
            MetricRow(K, "downside", "Downside deviation (annualised)", p.downside_deviation,
                      bv("downside_deviation"), PCT,
                      "Like volatility, but only the days below the risk-free rate count."),
        ]
    rows += [
        MetricRow(K, "mdd", "Maximum drawdown", p.max_drawdown, bv("max_drawdown"), PCT,
                  "The largest fall from a previous high of the time-weighted index."),
        MetricRow(K, "mdd_period", DRAWDOWN_PERIOD_LABEL, drawdown_period(p), drawdown_period(b) if b else None, TEXT,
                  "Peak date to trough date, and when the old high was reached again."),
        MetricRow(K, "current_dd", "Current drawdown", p.current_drawdown, bv("current_drawdown"), PCT,
                  "Distance from the highest point so far (0 = at a new high)."),
    ]
    if rep.status == OK:
        rows += [
            MetricRow(K, "var95", "Daily Value at Risk (95 %)", p.var95, bv("var95"), PCT,
                      "On 95 % of the days the loss was smaller than this (historical, 1 day)."),
            MetricRow(K, "cvar95", "Daily expected shortfall (95 %)", p.cvar95, bv("cvar95"), PCT,
                      "The average loss on the worst 5 % of the days."),
        ]
    rows += [
        MetricRow(K, "best", "Best day", p.best_day, bv("best_day"), PCT, ""),
        MetricRow(K, "worst", "Worst day", p.worst_day, bv("worst_day"), PCT, ""),
        MetricRow(K, "positive", "Days with a gain", p.positive_share, bv("positive_share"), PCT,
                  "Share of the trading days that ended above the previous one."),
    ]
    if rep.status == OK:
        rows += [
            MetricRow(X, "sharpe", "Sharpe ratio", p.sharpe, bv("sharpe"), RATIO,
                      "Return above the risk-free rate per unit of volatility (annualised). Above 1 is good."),
            MetricRow(X, "sortino", "Sortino ratio", p.sortino, bv("sortino"), RATIO,
                      "Like Sharpe, but only the downside deviation counts as risk."),
            MetricRow(X, "calmar", "Calmar ratio", p.calmar, bv("calmar"), RATIO,
                      "Annualised return / maximum drawdown."),
        ]
        if rel is not None:
            B = "Against the benchmark" + (f" ({rep.benchmark_symbol})" if rep.benchmark_symbol else "")
            rows += [
                MetricRow(B, "beta", "Beta", rel.beta, 1.0, RATIO,
                          "Sensitivity to the benchmark: 1.2 = the portfolio moves 20 % more than the benchmark."),
                MetricRow(B, "correlation", "Correlation", rel.correlation, 1.0, RATIO,
                          "How closely the daily returns follow the benchmark (1 = in lockstep)."),
                MetricRow(B, "alpha", "Alpha (Jensen, annualised)", rel.alpha, 0.0, PCT,
                          "Return beyond what the beta explains."),
                MetricRow(B, "tracking", "Tracking error (annualised)", rel.tracking_error, 0.0, PCT,
                          "Standard deviation of the daily difference to the benchmark."),
                MetricRow(B, "info", "Information ratio", rel.information_ratio, None, RATIO,
                          "Average daily excess return over the tracking error (annualised)."),
                MetricRow(B, "treynor", "Treynor ratio", rel.treynor, None, PCT,
                          "Return above the risk-free rate per unit of beta."),
            ]
    return rows
