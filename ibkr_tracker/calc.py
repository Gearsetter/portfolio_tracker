"""Python implementation of every number the workbook computes with formulas.

It serves two purposes: it feeds the GUI dashboard (so the app works without opening Excel) and it is an
independent oracle - the tests recalculate the workbook with LibreOffice and compare cell by cell.

Definitions (identical to the spreadsheet):

* ``Total = Quantity x Price`` (sells negative), ``Gross = Total + Fee`` (fee is a positive cost).
* USD conversion divides by *units of currency per USD* (``USD.HUF`` = HUF per USD).
* Section 5 columns M/N/O convert at the **purchase-date** rate, Section 6 column O at the **latest** rate.
* Time-weighted CAGR = ``Return ** (1 / weighted average years since deposits) - 1`` with the deposits'
  USD amounts as weights (interest rows are not contributions and are excluded).
* Share counts (Section 6, column K) are the **Open Positions of the latest statement**, never the transaction sum
  (stock splits!).  ``share_factor`` = statement shares / transaction sum converts the quantity of a transaction into
  today's shares for the "Current Total" columns, so that they add up to the value of the position.
* VUAA CAGR (USD): every stock transaction replayed into the benchmark ETF (Section 5, column P);
  ``(VUAA value / invested) ** (1 / purchase-weighted average years) - 1`` with the USD Totals as weights.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .market.data import MarketData
from .models import StockTrade
from .portfolio import Portfolio

CONTRIBUTION_EXCLUDED = {"interest"}


@dataclass
class TradeRow:
    trade: StockTrade
    total: float
    gross: float
    current_price: Optional[float]
    current_total: Optional[float]
    fx_rate: float                       # units of trade currency per USD on the purchase date
    usd_total: float
    usd_gross: float
    usd_current: Optional[float]         # current total converted at the purchase-date rate
    bench_close: Optional[float]
    vuaa_qty: Optional[float]
    share_factor: float = 1.0            # statement shares / transaction sum of this position (1 unless a split...)


@dataclass
class PositionRow:
    ticker: str
    currency: str
    name: str
    quantity: float
    price: Optional[float]
    value: float
    gross: float
    ret: Optional[float]                 # value / gross
    usd_value: float
    usd_gross: float
    usd_ret: Optional[float]
    tx_quantity: float = 0.0             # sum of the transactions (NOT the share count)
    share_factor: float = 1.0


@dataclass
class CashRow:
    currency: str
    balance: float
    usd_value: float


@dataclass
class DepositRow:
    kind: str
    description: str
    currency: str
    day: date
    amount: float
    rate: float                          # base currency units per USD on that day (the "USD.HUF" column)
    usd_amount: float
    base_amount: float
    account: str = ""


@dataclass
class BenchmarkResult:
    available: bool = False
    symbol: str = ""
    units: Optional[float] = None
    price: Optional[float] = None
    value_usd: Optional[float] = None
    invested_usd: float = 0.0
    stocks_value_const_fx: float = 0.0
    stocks_value_latest_fx: float = 0.0
    ret_stocks_const: Optional[float] = None
    ret_stocks_latest: Optional[float] = None
    ret_vuaa: Optional[float] = None
    excess_usd: Optional[float] = None
    years: Optional[float] = None            # purchase-weighted average years from the transactions to the as-of date
    cagr_vuaa: Optional[float] = None        # VUAA CAGR in USD  (the dashboard's primary benchmark figure)
    cagr_stocks: Optional[float] = None      # stocks CAGR on the very same basis (latest FX)


@dataclass
class Metrics:
    base_currency: str
    as_of: date
    trades: list[TradeRow]
    positions: list[PositionRow]
    cash: list[CashRow]
    deposits: list[DepositRow]
    stocks_usd: float
    cash_usd: float
    usd_total: float
    usd_deposits: float
    usd_return: Optional[float]
    years: Optional[float]
    usd_cagr: Optional[float]
    base_total: float
    base_deposits: float
    base_return: Optional[float]
    base_cagr: Optional[float]
    benchmark: BenchmarkResult = field(default_factory=BenchmarkResult)


def _cagr(ratio: Optional[float], years: Optional[float]) -> Optional[float]:
    if ratio is None or years is None or ratio <= 0 or years <= 0:
        return None
    return ratio ** (1.0 / years) - 1.0


def compute_metrics(portfolio: Portfolio, md: MarketData, days_per_year: float = 365.0) -> Metrics:
    base = portfolio.base_currency
    fx = md.fx

    held = portfolio.net_quantities()                  # share counts: Open Positions of the latest statement
    tx_qty = portfolio.transaction_quantities()
    factors = portfolio.share_factors()

    # ---- Section 1 + 5 (per transaction) -----------------------------------------------------------------
    trade_rows: list[TradeRow] = []
    for t in portfolio.trades:
        total = t.quantity * t.price
        gross = total + t.fee
        factor = factors.get((t.ticker, t.currency), 1.0)
        cur_price = md.price(t.ticker, t.currency)
        cur_total = t.quantity * factor * cur_price if cur_price is not None else None
        day = t.dt.date()
        rate = fx.rate(t.currency, day) if fx.has(t.currency) else float("nan")
        bench = md.benchmark.close(day)
        usd_total = total / rate
        trade_rows.append(TradeRow(
            trade=t, total=total, gross=gross, current_price=cur_price, current_total=cur_total, fx_rate=rate,
            usd_total=usd_total, usd_gross=gross / rate,
            usd_current=(cur_total / rate) if cur_total is not None else None,
            bench_close=bench, vuaa_qty=(usd_total / bench) if bench and bench > 0 else None, share_factor=factor))

    # ---- Section 6 (positions) --------------------------------------------------------------------------
    positions: list[PositionRow] = []
    for ticker, ccy in portfolio.positions_keys():
        rows = [r for r in trade_rows if r.trade.ticker == ticker and r.trade.currency == ccy]
        qty = held.get((ticker, ccy), 0.0)
        gross = sum(r.gross for r in rows)
        usd_gross = sum(r.usd_gross for r in rows)
        price = md.price(ticker, ccy)
        value = qty * (price or 0.0)
        usd_value = value / md.fx_used(ccy)
        positions.append(PositionRow(
            ticker=ticker, currency=ccy, name=portfolio.instrument_name(ticker), quantity=qty, price=price,
            value=value, gross=gross, ret=(value / gross) if qty != 0 and gross else None,
            usd_value=usd_value, usd_gross=usd_gross,
            usd_ret=(usd_value / usd_gross) if qty != 0 and usd_gross else None,
            tx_quantity=tx_qty.get((ticker, ccy), 0.0), share_factor=factors.get((ticker, ccy), 1.0)))

    # ---- Section 7 (cash) -------------------------------------------------------------------------------
    balances = portfolio.cash_balances()
    cash = [CashRow(c, balances.get(c, 0.0), balances.get(c, 0.0) / md.fx_used(c) if (c == "USD" or md.fx.has(c) or
                                                                                    c in md.fx_overrides) else 0.0)
            for c in portfolio.currencies()]

    # ---- Section 3 (deposits) ---------------------------------------------------------------------------
    deposits: list[DepositRow] = []
    for c in portfolio.cash_flows:
        day = c.dt.date()
        rate = fx.rate(base, day) if (base != "USD" and fx.has(base)) else 1.0
        if c.currency == "USD":
            usd = c.amount
        elif c.currency == base:
            usd = c.amount / rate
        else:
            usd = c.amount / fx.rate(c.currency, day)
        base_amt = c.amount if c.currency == base else usd * rate
        deposits.append(DepositRow(c.kind, c.description, c.currency, day, c.amount, rate, usd, base_amt, c.account))

    contributions = [d for d in deposits if d.kind not in CONTRIBUTION_EXCLUDED]
    usd_dep = sum(d.usd_amount for d in contributions)
    base_dep = sum(d.base_amount for d in contributions)
    years = None
    if usd_dep:
        years = sum(d.usd_amount * (md.as_of - d.day).days for d in contributions) / usd_dep / days_per_year

    stocks_usd = sum(p.usd_value for p in positions)
    cash_usd = sum(c.usd_value for c in cash)
    usd_total = stocks_usd + cash_usd
    usd_ret = usd_total / usd_dep if usd_dep else None
    base_rate_latest = md.fx_used(base) if base != "USD" else 1.0
    base_total = usd_total * base_rate_latest
    base_ret = base_total / base_dep if base_dep else None

    # ---- benchmark ---------------------------------------------------------------------------------------
    bench = BenchmarkResult(symbol=md.benchmark.symbol)
    invested = sum(r.usd_total for r in trade_rows)
    const_fx = sum((r.usd_current or 0.0) for r in trade_rows)
    bench.invested_usd = invested
    bench.stocks_value_const_fx = const_fx
    bench.stocks_value_latest_fx = stocks_usd
    bench_price = md.benchmark_price()
    if bench_price and trade_rows and all(r.vuaa_qty is not None for r in trade_rows):
        bench.available = True
        bench.units = sum(r.vuaa_qty for r in trade_rows)             # type: ignore[misc]
        bench.price = bench_price
        bench.value_usd = bench.units * bench_price
        bench.excess_usd = stocks_usd - bench.value_usd
        bench.ret_vuaa = bench.value_usd / invested if invested else None
    bench.ret_stocks_const = const_fx / invested if invested else None
    bench.ret_stocks_latest = stocks_usd / invested if invested else None
    if invested:
        bench.years = sum(r.usd_total * (md.as_of - r.trade.dt.date()).days for r in trade_rows) / invested / days_per_year
    bench.cagr_vuaa = _cagr(bench.ret_vuaa, bench.years)
    bench.cagr_stocks = _cagr(bench.ret_stocks_latest, bench.years)

    return Metrics(
        base_currency=base, as_of=md.as_of, trades=trade_rows, positions=positions, cash=cash, deposits=deposits,
        stocks_usd=stocks_usd, cash_usd=cash_usd, usd_total=usd_total, usd_deposits=usd_dep, usd_return=usd_ret,
        years=years, usd_cagr=_cagr(usd_ret, years), base_total=base_total, base_deposits=base_dep,
        base_return=base_ret, base_cagr=_cagr(base_ret, years), benchmark=bench)
