"""Day-by-day reconstruction of the portfolio over its **whole lifespan** (first transaction -> valuation date).

The dashboard used to chart the numbers recorded at each *Refresh Prices*; that says nothing about the time before
the first refresh and depends on when the user happened to press the button.  This module replays the ledger instead:

for every calendar day ``d`` from the first transaction to the valuation date

* **shares** of each stock = the transactions up to ``d`` expressed in today's shares (``share_factor`` - the Open
  Positions of the newest statement are the source of truth, so a split is handled like in Section 6) plus - for
  shares no transaction explains (transfers, spin-offs, history older than the archive) - an opening balance;
* **cash** per currency = the ledger (deposits, withdrawals, fees, interest, dividends net of tax, stock trades with
  their commissions and the forex conversions) up to ``d`` - the same formula as Section 7;
* **prices** = the merged daily series of the stock (manual price history > Yahoo Finance > statement closes, see
  :mod:`ibkr_tracker.market.service`), each day carried forward from the last available close;
* **FX** = the rate of the day (last known on or before ``d``), exactly like the transaction-date conversions of the
  workbook;
* **net deposits** = deposits, withdrawals and fees converted at the rate of *their* day (interest is not a
  contribution) - the base of the return and of the time-weighted CAGR;
* the **VUAA benchmark**: every stock purchase / sale replayed into the benchmark ETF on its day (Section 5 column P),
  valued at the benchmark close of ``d``.

On the last day the numbers are those of the point-in-time calculation (:func:`ibkr_tracker.calc.compute_metrics`):
the current price, the manual overrides and the latest FX rate are used there, so the final point of every chart equals
the KPI tiles (``History.matches``).

What happens when data are missing (all of it is reported in ``History.coverage`` / ``History.notes``):

* a held stock has **no price on or before a day** (it is not at Yahoo, the manual history starts later ...): the day
  is valued at the price of the stock's **latest purchase / sale** - a flat, honest estimate, never zero;
* the newest price is **older than** :data:`STALE_DAYS` days (delisting, a manual history that ends early): the last
  known close is carried forward and the day counts as *stale*;
* the **FX series** is missing for a currency: that currency is left out and a note says so.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from .calc import CONTRIBUTION_EXCLUDED, Metrics, _cagr, compute_metrics
from .market.data import SRC_MANUAL, SRC_STATEMENT, SRC_YAHOO, MarketData, PriceSeries
from .portfolio import QTY_TOL, Portfolio

STALE_DAYS = 10                 # carrying a price forward for longer than this is reported as "stale"
MIN_CAGR_YEARS = 0.25           # charts hide the annualised figures of a portfolio that is younger than this
MAX_NOTES = 12
EPS = 1e-9

# column name -> (label used by the GUI / CSV / Excel, unit)
COLUMNS: dict[str, tuple[str, str]] = {
    "stocks_usd": ("Stocks (USD)", "USD"),
    "cash_usd": ("Cash (USD)", "USD"),
    "total_usd": ("Total value (USD)", "USD"),
    "deposits_usd": ("Net deposits (USD)", "USD"),
    "gain_usd": ("Gain (USD)", "USD"),
    "return_usd": ("Return multiple (USD)", "x"),
    "cagr_usd": ("CAGR (USD)", "fraction"),
    "base_per_usd": ("Base currency per USD", "rate"),
    "total_base": ("Total value (base)", "base"),
    "deposits_base": ("Net deposits (base)", "base"),
    "gain_base": ("Gain (base)", "base"),
    "return_base": ("Return multiple (base)", "x"),
    "cagr_base": ("CAGR (base)", "fraction"),
    "years": ("Weighted years since deposits", "years"),
    "invested_usd": ("Stock purchases net (USD)", "USD"),
    "bench_value_usd": ("Benchmark value (USD)", "USD"),
    "bench_excess_usd": ("Stocks minus benchmark (USD)", "USD"),
    "bench_years": ("Weighted years since the stock purchases", "years"),
    "ret_stocks": ("Stocks return multiple", "x"),
    "ret_bench": ("Benchmark return multiple", "x"),
    "cagr_stocks": ("Stocks CAGR (USD)", "fraction"),
    "cagr_bench": ("Benchmark CAGR (USD)", "fraction"),
    "n_positions": ("Positions held", "count"),
}


@dataclass
class PositionCoverage:
    """How well the price data of one stock cover the days it was held."""
    ticker: str
    currency: str
    held_days: int = 0
    first_held: Optional[date] = None
    last_held: Optional[date] = None
    points: int = 0                      # prices in the merged series
    yahoo_points: int = 0
    manual_points: int = 0
    statement_points: int = 0
    first_price: Optional[date] = None
    last_price: Optional[date] = None
    fallback_days: int = 0               # held days valued at the last purchase / sale price (no price data yet)
    stale_days: int = 0                  # held days valued with a price older than STALE_DAYS
    missing_days: int = 0                # held days without any price at all (valued at zero)
    opening_shares: float = 0.0          # shares that no transaction in the archive explains
    share_factor: float = 1.0

    @property
    def key(self) -> str:
        return f"{self.ticker}|{self.currency}"

    @property
    def complete(self) -> bool:
        """True when every held day was valued with a real, recent price."""
        return not (self.fallback_days or self.stale_days or self.missing_days)

    @property
    def status(self) -> str:
        if not self.held_days:
            return "never held in the period"
        if self.complete:
            return "complete"
        bits = []
        if self.fallback_days:
            bits.append(f"{self.fallback_days} day(s) at the last trade price")
        if self.stale_days:
            bits.append(f"{self.stale_days} day(s) with an old price")
        if self.missing_days:
            bits.append(f"{self.missing_days} day(s) without price")
        return "; ".join(bits)


@dataclass
class History:
    base_currency: str
    start: Optional[date]
    end: Optional[date]
    frame: pd.DataFrame                              # one row per calendar day, indexed by Date (see COLUMNS)
    positions: pd.DataFrame                          # USD value per stock and day (columns = stock labels)
    coverage: list[PositionCoverage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    benchmark_symbol: str = ""
    benchmark_available: bool = False
    benchmark_priced: bool = False                   # the benchmark has prices (False = nothing could be downloaded)

    @property
    def empty(self) -> bool:
        return self.frame.empty

    @property
    def days(self) -> int:
        return len(self.frame)

    @property
    def last(self) -> pd.Series:
        return self.frame.iloc[-1]

    def value(self, day: date, column: str = "total_usd") -> float:
        """Value of ``column`` on ``day`` (the row of the nearest earlier day when ``day`` is not in the range)."""
        ts = pd.Timestamp(day)
        i = self.frame.index.searchsorted(ts, side="right") - 1
        return float(self.frame[column].iloc[max(i, 0)])

    def since(self, first: Optional[date]) -> pd.DataFrame:
        """The frame restricted to the days from ``first`` on (``None`` = the whole lifespan)."""
        if first is None or self.empty:
            return self.frame
        return self.frame.loc[self.frame.index >= pd.Timestamp(first)]

    def matches(self, m: Metrics, tol: float = 0.01) -> list[str]:
        """Differences between the last day and the point-in-time metrics (empty list = they agree).  They only
        disagree when a held stock has no market price at all (the KPI values it at zero, the history at the last
        trade price) - see the coverage report."""
        if self.empty:
            return ["the history is empty"]
        last, out = self.last, []
        for name, mine, theirs in (("stocks (USD)", last["stocks_usd"], m.stocks_usd),
                                   ("cash (USD)", last["cash_usd"], m.cash_usd),
                                   ("total (USD)", last["total_usd"], m.usd_total),
                                   ("deposits (USD)", last["deposits_usd"], m.usd_deposits),
                                   ("total (base)", last["total_base"], m.base_total)):
            if not (math.isnan(mine) and math.isnan(theirs)) and abs(mine - theirs) > tol:
                out.append(f"{name}: history {mine:,.2f} vs KPI {theirs:,.2f}")
        return out

    def coverage_for(self, ticker: str, currency: str) -> Optional[PositionCoverage]:
        for c in self.coverage:
            if c.ticker == ticker and c.currency == currency:
                return c
        return None


# ---------------------------------------------------------------------------------------------------------------
def empty_history(portfolio: Portfolio, md: Optional[MarketData] = None) -> History:
    return History(portfolio.base_currency, None, None, pd.DataFrame(columns=list(COLUMNS)),
                   pd.DataFrame(), benchmark_symbol=md.benchmark.symbol if md else "")


@dataclass
class _Pos:
    """Replay state of one stock."""
    ticker: str
    ccy: str
    factor: float
    offset: float
    qty_events: dict[date, float]
    trade_prices: list[tuple[date, float]]           # (day, price in today's share terms), ascending
    series: Optional[PriceSeries]
    cov: PositionCoverage
    cum: float = 0.0
    sidx: int = 0                                    # points of the series consumed so far
    tidx: int = 0                                    # trade prices consumed so far
    last_trade_px: Optional[float] = None


def build_history(portfolio: Portfolio, md: MarketData, metrics: Optional[Metrics] = None,
                  days_per_year: float = 365.0) -> History:
    """Reconstruct the portfolio day by day (see the module docstring).  ``metrics`` are the point-in-time numbers
    of the same ``portfolio`` / ``md`` (computed here when not given): their deposit and trade rows - converted at
    the rates of their own days - are replayed, so history and KPIs cannot drift apart."""
    m = metrics if metrics is not None else compute_metrics(portfolio, md, days_per_year)
    base = portfolio.base_currency
    first = portfolio.first_activity()
    if first is None:
        return empty_history(portfolio, md)

    # ---- events by day -----------------------------------------------------------------------------------------
    cash_ev: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    for c in portfolio.cash_flows:
        cash_ev[c.currency][c.dt.date()] += c.amount
    for d in portfolio.dividends:
        cash_ev[d.currency][d.dt] += d.total - d.tax_fee
    for t in portfolio.trades:
        cash_ev[t.currency][t.dt.date()] -= t.quantity * t.price + t.fee
    for f in portfolio.fx_trades:
        day = f.dt.date()
        cash_ev[f.bought][day] += f.quantity
        cash_ev[f.paid][day] -= f.quantity * f.price
        cash_ev[f.fee_currency][day] -= f.fee

    event_days = [t.dt.date() for t in portfolio.trades] + [f.dt.date() for f in portfolio.fx_trades] \
        + [c.dt.date() for c in portfolio.cash_flows] + [d.dt for d in portfolio.dividends]
    end = max([md.as_of, *event_days])
    start = min(first, end)

    dep_ev: dict[date, tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))       # day -> (usd, base)
    for d in m.deposits:
        if d.kind not in CONTRIBUTION_EXCLUDED:
            u, b = dep_ev[d.day]
            dep_ev[d.day] = (u + d.usd_amount, b + d.base_amount)

    bench_ev: dict[date, tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))     # day -> (usd invested, units)
    bench_ok = bool(m.trades) and all(r.vuaa_qty is not None for r in m.trades)
    for r in m.trades:
        u, q = bench_ev[r.trade.dt.date()]
        bench_ev[r.trade.dt.date()] = (u + r.usd_total, q + (r.vuaa_qty or 0.0))

    # ---- stocks ------------------------------------------------------------------------------------------------
    held = portfolio.net_quantities()
    tx_sum = portfolio.transaction_quantities()
    by_key: dict[tuple[str, str], list] = defaultdict(list)
    for t in portfolio.trades:
        by_key[(t.ticker, t.currency)].append(t)
    pos: list[_Pos] = []
    for ticker, ccy in portfolio.positions_keys():
        key = (ticker, ccy)
        tx, h = tx_sum.get(key, 0.0), held.get(key, 0.0)
        factor = h / tx if abs(tx) > QTY_TOL and h / tx > 0 else 1.0         # a split: statement shares / trade sum
        offset = h - factor * tx                                              # shares no transaction explains
        if abs(offset) < QTY_TOL:
            offset = 0.0
        events: dict[date, float] = defaultdict(float)
        trade_px: list[tuple[date, float]] = []
        for t in sorted(by_key.get(key, []), key=lambda x: x.dt):
            events[t.dt.date()] += t.quantity
            trade_px.append((t.dt.date(), t.price / factor))
        ser = md.price_series.get(f"{ticker}|{ccy}")
        cov = PositionCoverage(ticker, ccy, opening_shares=offset, share_factor=factor)
        if ser is not None and len(ser):
            cov.points, cov.first_price, cov.last_price = len(ser), ser.first, ser.last
            cov.yahoo_points, cov.manual_points = ser.count(SRC_YAHOO), ser.count(SRC_MANUAL)
            cov.statement_points = ser.count(SRC_STATEMENT)
        pos.append(_Pos(ticker, ccy, factor, offset, events, trade_px, ser, cov))

    # ---- currency helpers -------------------------------------------------------------------------------------
    fx_missing: set[str] = set()

    def rate(ccy: str, day: date, last: bool) -> Optional[float]:
        """Units of ``ccy`` per USD on ``day`` (the latest rate - overrides included - on the last day)."""
        if ccy == "USD":
            return 1.0
        try:
            if last:
                return md.fx_used(ccy)
            return md.fx.rate(ccy, day) if md.fx.has(ccy) else md.fx_used(ccy)
        except KeyError:
            return None

    # ---- the day loop ----------------------------------------------------------------------------------------
    days: list[date] = []
    rows: dict[str, list[float]] = {c: [] for c in COLUMNS}
    pos_usd: dict[str, list[float]] = {f"{p.ticker}|{p.ccy}": [] for p in pos}
    balances: dict[str, float] = defaultdict(float)
    s0 = s1 = b0 = 0.0                                  # net deposits: sum usd, sum usd*ordinal, sum base
    t0 = t1 = units = 0.0                               # benchmark replay: invested usd, usd*ordinal, ETF units
    nan = float("nan")
    d = start
    while d <= end:
        last = d == end
        # events of the day
        for ccy, ev in cash_ev.items():
            if d in ev:
                balances[ccy] += ev[d]
        if d in dep_ev:
            u, b = dep_ev[d]
            s0 += u
            s1 += u * d.toordinal()
            b0 += b
        if d in bench_ev:
            u, q = bench_ev[d]
            t0 += u
            t1 += u * d.toordinal()
            units += q

        # stocks
        stocks_usd, n_held = 0.0, 0
        for p in pos:
            p.cum += p.qty_events.get(d, 0.0)
            while p.tidx < len(p.trade_prices) and p.trade_prices[p.tidx][0] <= d:
                p.last_trade_px = p.trade_prices[p.tidx][1]
                p.tidx += 1
            qty = p.factor * p.cum + p.offset
            value_usd = 0.0
            if abs(qty) >= QTY_TOL:
                n_held += 1
                cov = p.cov
                cov.held_days += 1
                cov.first_held = cov.first_held or d
                cov.last_held = d
                price: Optional[float] = None
                if last:
                    price = md.price(p.ticker, p.ccy)
                if price is None:
                    price = _carry_forward(p, d, cov)
                r = rate(p.ccy, d, last)
                if price is not None and r:
                    value_usd = qty * price / r
                elif r is None and p.ccy not in fx_missing:
                    fx_missing.add(p.ccy)
            stocks_usd += value_usd
            pos_usd[f"{p.ticker}|{p.ccy}"].append(value_usd)

        # cash
        cash_usd = 0.0
        for ccy, bal in balances.items():
            r = rate(ccy, d, last)
            if r:
                cash_usd += bal / r
            elif abs(bal) > 1e-9 and ccy not in fx_missing:
                fx_missing.add(ccy)
        total_usd = stocks_usd + cash_usd

        # returns (a zero or vanishing sum of deposits has no return)
        dep = abs(s0) > EPS
        years = (d.toordinal() * s0 - s1) / s0 / days_per_year if dep else nan
        ret_usd = total_usd / s0 if dep else nan
        base_r = rate(base, d, last) if base != "USD" else 1.0
        total_base = total_usd * base_r if base_r else nan
        ret_base = total_base / b0 if (abs(b0) > EPS and not math.isnan(total_base)) else nan

        # benchmark: the stock purchases replayed into the ETF
        bench_value = nan
        if bench_ok:
            price_b = md.benchmark_price() if last else md.benchmark.close(d)
            if price_b:
                bench_value = units * price_b
        inv = abs(t0) > EPS
        years_b = (d.toordinal() * t0 - t1) / t0 / days_per_year if inv else nan
        ret_stocks = stocks_usd / t0 if inv else nan
        ret_bench = bench_value / t0 if (inv and not math.isnan(bench_value)) else nan

        days.append(d)
        rows["stocks_usd"].append(stocks_usd)
        rows["cash_usd"].append(cash_usd)
        rows["total_usd"].append(total_usd)
        rows["deposits_usd"].append(s0)
        rows["gain_usd"].append(total_usd - s0)
        rows["return_usd"].append(ret_usd)
        rows["cagr_usd"].append(_nan(_cagr(_none(ret_usd), _none(years))))
        rows["base_per_usd"].append(base_r if base_r else nan)
        rows["total_base"].append(total_base)
        rows["deposits_base"].append(b0)
        rows["gain_base"].append(total_base - b0 if not math.isnan(total_base) else nan)
        rows["return_base"].append(ret_base)
        rows["cagr_base"].append(_nan(_cagr(_none(ret_base), _none(years))))
        rows["years"].append(years)
        rows["invested_usd"].append(t0)
        rows["bench_value_usd"].append(bench_value)
        rows["bench_excess_usd"].append(stocks_usd - bench_value if not math.isnan(bench_value) else nan)
        rows["bench_years"].append(years_b)
        rows["ret_stocks"].append(ret_stocks)
        rows["ret_bench"].append(ret_bench)
        rows["cagr_stocks"].append(_nan(_cagr(_none(ret_stocks), _none(years_b))))
        rows["cagr_bench"].append(_nan(_cagr(_none(ret_bench), _none(years_b))))
        rows["n_positions"].append(float(n_held))
        d += timedelta(days=1)

    index = pd.DatetimeIndex([pd.Timestamp(x) for x in days], name="Date")
    frame = pd.DataFrame(rows, index=index)

    # per-stock USD values: only stocks that were worth something at some point, labelled by ticker
    tickers = [k.split("|")[0] for k in pos_usd]
    labels = {k: (k.split("|")[0] if tickers.count(k.split("|")[0]) == 1 else f"{k.split('|')[0]} ({k.split('|')[1]})")
              for k in pos_usd}
    positions = pd.DataFrame({labels[k]: v for k, v in pos_usd.items() if any(abs(x) > 1e-9 for x in v)}, index=index)

    priced = bool(md.benchmark.dates)
    hist = History(base, start, end, frame, positions, [p.cov for p in pos if p.cov.held_days or p.cov.points],
                   benchmark_symbol=md.benchmark.symbol, benchmark_available=bench_ok and priced,
                   benchmark_priced=priced)
    downloaded = any(SRC_YAHOO in ser.sources or SRC_MANUAL in ser.sources for ser in md.price_series.values())
    hist.notes = _notes(hist, fx_missing, downloaded)
    return hist


def _none(x: float) -> Optional[float]:
    return None if (x is None or (isinstance(x, float) and math.isnan(x))) else x


def _nan(x: Optional[float]) -> float:
    return float("nan") if x is None else x


def _carry_forward(p: _Pos, d: date, cov: PositionCoverage) -> Optional[float]:
    """The price to value a held stock on ``d`` with: the last close on or before ``d`` (carried forward); before the
    first close - or without any - the stock's latest purchase / sale price."""
    ser = p.series
    if ser is not None and len(ser):
        while p.sidx < len(ser.dates) and ser.dates[p.sidx] <= d:
            p.sidx += 1
        if p.sidx > 0:
            if (d - ser.dates[p.sidx - 1]).days > STALE_DAYS:
                cov.stale_days += 1
            return ser.closes[p.sidx - 1]
    if p.last_trade_px is not None:
        cov.fallback_days += 1
        return p.last_trade_px
    if p.trade_prices:                                    # opening shares, nothing traded yet: the first known price
        cov.fallback_days += 1
        return p.trade_prices[0][1]
    cov.missing_days += 1
    return None


def _notes(h: History, fx_missing: set[str], downloaded: bool = True) -> list[str]:
    """What the reader should know about the quality of the curve - one note per kind of problem (not per stock),
    so a first import does not drown the dashboard in near-identical warnings."""
    held = [c for c in h.coverage if c.held_days]
    lab = lambda c: f"{c.ticker} ({c.currency})"                                                   # noqa: E731
    out: list[str] = []
    fall = [c for c in held if c.fallback_days]
    if fall and not downloaded:
        out.append("No daily prices have been downloaded yet (only the closing prices printed in the statements): "
                   "before a statement date every stock is valued at its last purchase / sale price. Press 'Refresh "
                   "Prices' for the daily closes - or add prices under Market data > Manual price history.")
    elif fall:
        bits = [f"{lab(c)} {c.fallback_days} of {c.held_days} day(s)"
                + (f", first price {c.first_price}" if c.first_price else ", no price data at all") for c in fall]
        out.append("Days without any price are valued at the last purchase / sale price - " + "; ".join(bits) +
                   ". Add the missing prices under Market data > Manual price history.")
    if stale := [c for c in held if c.stale_days]:
        out.append(f"Prices older than {STALE_DAYS} days are carried forward (a gap or the end of the price data) - " +
                   "; ".join(f"{lab(c)} {c.stale_days} day(s)" for c in stale) +
                   ". The coverage table shows where the prices start and end; missing ones can be added under "
                   "Market data > Manual price history.")
    if miss := [c for c in held if c.missing_days]:
        out.append("Days that could not be valued (no price and no trade) are counted as zero - " +
                   "; ".join(f"{lab(c)} {c.missing_days} day(s)" for c in miss) + ".")
    if opening := [c for c in held if abs(c.opening_shares) > QTY_TOL]:
        out.append("Shares with no transaction in the imported statements (a transfer, a spin-off or history older "
                   "than the archive) are assumed held from the first day - " +
                   "; ".join(f"{lab(c)} {c.opening_shares:,.4g}" for c in opening) + ".")
    for ccy in sorted(fx_missing):
        out.append(f"No FX rates for {ccy}: amounts in {ccy} are left out of the history.")
    name = h.benchmark_symbol or "the benchmark"
    if not h.benchmark_priced:
        out.append(f"No benchmark prices ({name}): the benchmark curve is not drawn.")
    elif not h.benchmark_available:
        out.append(f"No stock purchases or sales in the imported statements: there is nothing to replay into {name}, "
                   "so the benchmark curve is not drawn.")
    if len(out) > MAX_NOTES:
        out = out[:MAX_NOTES] + [f"... and {len(out) - MAX_NOTES} more note(s)."]
    return out
