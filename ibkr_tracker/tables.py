"""pandas views of the results (for the GUI).  Column names mirror the spreadsheet headers."""
from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd

from . import analytics
from .allocation import GROUPINGS, Allocation, Concentration, Holding
from .calc import Metrics
from .history import COLUMNS as HISTORY_COLUMNS
from .history import MIN_CAGR_YEARS, History
from .portfolio import Portfolio, is_closed
from .reconcile import CheckRow


def _num(v) -> float:
    try:
        return float(v) if v not in (None, "") else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def _with_account(df: pd.DataFrame, accounts: list[str], enabled: bool) -> pd.DataFrame:
    """Consolidated view: first column says which account a record comes from."""
    if enabled:
        df.insert(0, "Account", accounts)
    return df


def trades_table(m: Metrics, with_account: bool = False) -> pd.DataFrame:
    rows = []
    for r in m.trades:
        t = r.trade
        rows.append({
            "Name": t.name, "Ticker": t.ticker, "Quantity": t.quantity, "Price": t.price, "Total": r.total,
            "Fee": t.fee, "Currency": t.currency, "Date": t.dt, "Gross": r.gross, "Current Price": r.current_price,
            "Current Total": r.current_total, "USD Total": r.usd_total, "USD Gross": r.usd_gross,
            "USD Current Total": r.usd_current, "VUAA Quantity": r.vuaa_qty})
    return _with_account(pd.DataFrame(rows), [r.trade.account for r in m.trades], with_account)


def dividends_table(p: Portfolio, with_account: bool = False) -> pd.DataFrame:
    df = pd.DataFrame([{
        "Name": d.name, "Ticker": d.ticker, "Quantity": d.quantity, "Dividend": d.per_share, "Total": d.total,
        "Withholding Tax": d.tax_fee, "Currency": d.currency, "Date": d.dt} for d in p.dividends])
    return _with_account(df, [d.account for d in p.dividends], with_account)


def deposits_table(m: Metrics, with_account: bool = False) -> pd.DataFrame:
    base = m.base_currency
    df = pd.DataFrame([{
        "Description": d.kind, "Amount": d.amount, f"USD.{base}": d.rate, "USD Amount": d.usd_amount, "Date": d.day,
        "Currency": d.currency, f"Amount ({base})": d.base_amount} for d in m.deposits])
    return _with_account(df, [d.account for d in m.deposits], with_account)


def forex_table(p: Portfolio, with_account: bool = False) -> pd.DataFrame:
    df = pd.DataFrame([{
        "Currency Bought": f.bought, "Quantity": f.quantity, "Price": f.price, "Total": f.quantity * f.price,
        "Fee": f.fee, "Currency Paid": f.paid, "Date": f.dt, "Fee Currency": f.fee_currency} for f in p.fx_trades])
    return _with_account(df, [f.account for f in p.fx_trades], with_account)


def positions_table(m: Metrics, hide: bool = False) -> pd.DataFrame:
    """Section 6.  ``Quantity`` is the share count of the latest statement's Open Positions; the last two columns
    show the sum of the transactions and the resulting factor (differs from 1 after a stock split, a transfer...).
    ``hide`` leaves out the fully closed positions (zero shares)."""
    return pd.DataFrame([{
        "Ticker": r.ticker, "Name": r.name, "Currency": r.currency, "Quantity": r.quantity,
        "Current Price": r.price, "Value": r.value, "Return": r.ret, "USD Value": r.usd_value,
        "USD Return": r.usd_ret, "CAGR": None, "Quantity (transactions)": r.tx_quantity,
        "Share factor": r.share_factor} for r in m.visible_positions(hide)])


def cash_table(m: Metrics, hide: bool = False) -> pd.DataFrame:
    """Section 7.  ``hide`` leaves out the dust balances (absolute amount below 0.5)."""
    return pd.DataFrame([{"Currency": c.currency, "Net cash": c.balance, "USD Value": c.usd_value}
                         for c in m.visible_cash(hide)])


def hidden_note(m: Metrics, hide: bool, part: str = "all") -> str:
    """One line saying what the view filter left out ('' when nothing was hidden).  ``part``: ``"closed"`` (positions
    table), ``"dust"`` (cash table) or ``"all"``."""
    h = m.hidden(hide)
    parts = []
    if h.closed and part in ("all", "closed"):
        parts.append(f"{len(h.closed)} closed position{'s' if len(h.closed) != 1 else ''}")
    if h.dust and part in ("all", "dust"):
        parts.append(f"{len(h.dust)} cash balance{'s' if len(h.dust) != 1 else ''} below 0.5 "
                     f"(together {h.dust_usd:,.2f} USD)")
    if not parts:
        return ""
    return "Hidden by the filter: " + " and ".join(parts) + ". The totals, returns and CAGR above still include them."


def checks_table(rows: list[CheckRow]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Check": r.check, "Item": r.item, "IBKR statement": r.ibkr, "Workbook": r.workbook,
        "Difference": r.difference, "Tolerance": r.tolerance, "Status": r.status} for r in rows])


def snapshots_table(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["as_of"] = pd.to_datetime(df["as_of"])
    return df


def prices_table(md, portfolio: Portfolio, settings: dict) -> pd.DataFrame:
    """Status of every price the valuation uses (GUI *Market data* tab)."""
    rows = []
    held = portfolio.net_quantities()
    for ticker, ccy in portfolio.positions_keys():
        key = f"{ticker}|{ccy}"
        raw = md.raw_prices.get(key)
        used = md.prices.get(key)
        no_price = ("not needed (position closed)" if is_closed(held.get((ticker, ccy), 0.0))
                    else "MISSING - refresh, add manual prices or enter an override")          # zero shares are worth nothing, price or not
        rows.append({
            "Ticker": ticker, "Currency": ccy, "Yahoo symbol": md.yahoo_symbols.get(key, ticker),
            "Last close": raw.price if raw else None, "Price date": raw.day if raw else None,
            "Source": raw.source if raw else no_price,
            "Override": _num((settings.get("price_overrides") or {}).get(key)),
            "Price used": used.price if used else None})
    return pd.DataFrame(rows)


def fx_status_table(md, portfolio: Portfolio, settings: dict) -> pd.DataFrame:
    rows = []
    active = set(portfolio.active_currencies())
    for ccy in portfolio.currencies():
        if ccy == "USD":
            continue
        raw = md.fx_latest_raw.get(ccy)
        over = (settings.get("fx_overrides") or {}).get(ccy)
        rows.append({
            "Currency": ccy, "Last rate (per USD)": raw.price if raw else None, "Rate date": raw.day if raw else None,
            "Source": raw.source if raw else ("MISSING - refresh, add manual prices or enter an override" if ccy in active
                                              else "not needed (no activity in this currency)"),
            "Override": _num(over),
            "Rate used": over if over not in (None, "") else (raw.price if raw else None)})
    return pd.DataFrame(rows)


def accounts_table(rows) -> pd.DataFrame:
    """Per-account breakdown of the consolidated view (``Snapshot.accounts``)."""
    return pd.DataFrame([{
        "Account": a.account, "Alias": a.alias, "Base currency": a.base_currency, "Statement to": a.period_end,
        "Stocks (USD)": a.stocks_usd, "Cash (USD)": a.cash_usd, "Total value (USD)": a.usd_total,
        "Share of total": a.share, "Net deposits (USD)": a.usd_deposits, "Return (USD)": a.usd_return,
        "CAGR (USD)": a.usd_cagr, "Total value (account base)": a.base_total, "Stock trades": a.n_trades}
        for a in rows])


def summary_table(m: Metrics, p: Portfolio, hide: bool = False) -> pd.DataFrame:
    """Sections 8-10 as plain rows (Excel / CSV export of the headline figures).  With the view filter on, two rows
    say what was left out of the position / cash lists (the figures themselves always include it)."""
    b = m.benchmark
    rows = [
        ("Portfolio", p.label, ""),
        ("Valuation date", m.as_of, ""),
        ("Total value", m.usd_total, "USD"), ("Net deposits", m.usd_deposits, "USD"),
        ("Return", m.usd_return, "x"), ("CAGR", m.usd_cagr, "fraction"),
        ("Stocks", m.stocks_usd, "USD"), ("Cash", m.cash_usd, "USD"),
        (f"Total value ({m.base_currency})", m.base_total, m.base_currency),
        (f"Net deposits ({m.base_currency})", m.base_deposits, m.base_currency),
        (f"Return ({m.base_currency})", m.base_return, "x"), (f"CAGR ({m.base_currency})", m.base_cagr, "fraction"),
        ("Average years since the deposits", m.years, "years"),
        (f"{b.symbol or 'Benchmark'} CAGR (USD)", b.cagr_vuaa, "fraction"),
        (f"{b.symbol or 'Benchmark'} value instead of the stock purchases", b.value_usd, "USD"),
        (f"{b.symbol or 'Benchmark'} return", b.ret_vuaa, "x"),
        ("Stocks CAGR on the same basis (USD)", b.cagr_stocks, "fraction"),
        ("Stocks minus benchmark (USD)", b.excess_usd, "USD"),
        ("Average years since the stock purchases", b.years, "years"),
    ]
    h = m.hidden(hide)
    if h:
        rows += [("Hidden closed positions", len(h.closed), "positions"),
                 ("Hidden dust cash balances (included in the totals)", h.dust_usd, "USD")]
    return pd.DataFrame(rows, columns=["Item", "Value", "Unit"])


# ---------------------------------------------------------------------------------------------------------------
# day-by-day history (dashboard charts, history.csv, Excel History sheet)
# ---------------------------------------------------------------------------------------------------------------
def history_label(column: str, base: str) -> str:
    """Display name of a history column; ``(base)`` becomes the base currency of the portfolio."""
    return HISTORY_COLUMNS[column][0].replace("(base)", f"({base})").replace("Base currency per USD", f"{base} per USD")


_BASE_ONLY = ("total_base", "deposits_base", "gain_base", "return_base", "cagr_base", "base_per_usd")


def history_columns(h: History) -> list[str]:
    """The frame columns that go into ``history.csv`` and the *History* sheet - a USD-based portfolio has no separate
    base-currency set (it would repeat the USD columns under the same names)."""
    skip = _BASE_ONLY if h.base_currency == "USD" else ()
    return [c for c in h.frame.columns if c not in skip]


def history_table(h: History) -> pd.DataFrame:
    """One row per calendar day with every reconstructed figure (what ``history.csv`` and the Excel *History* sheet
    hold).  The last columns give the value of every stock in USD.  Annualised figures (CAGR) stay empty until the
    deposits / purchases are on average :data:`~ibkr_tracker.history.MIN_CAGR_YEARS` (a quarter of a year) old."""
    if h.empty:
        return pd.DataFrame()
    cols = history_columns(h)
    df = h.frame[cols].copy()
    for col, years in (("cagr_usd", "years"), ("cagr_base", "years"), ("cagr_stocks", "bench_years"),
                       ("cagr_bench", "bench_years")):               # annualising a few weeks says nothing: leave empty
        if col in df:
            df[col] = df[col].where(h.frame[years] >= MIN_CAGR_YEARS)
    df.columns = [history_label(c, h.base_currency) for c in df.columns]
    df.insert(0, "Date", [d.date() for d in df.index])
    df = df.reset_index(drop=True)
    for name in h.positions.columns:
        df[f"{name} (USD)"] = h.positions[name].to_numpy()
    return df


def history_chart_frame(h: History, kind: str, unit: str = "base", since: Optional[date] = None) -> pd.DataFrame:
    """Data of one dashboard chart: ``kind`` is ``value`` (value vs net deposits), ``gain``, ``return`` (value / net
    deposits), ``cagr``, ``benchmark`` (stocks vs the benchmark replay, USD), ``benchmark_cagr`` or ``allocation``
    (stacked value of every stock and the cash).  ``unit``: ``"base"`` (base currency) or ``"usd"``;
    ``since`` restricts the days.  Annualised figures are left empty while the portfolio is younger than
    :data:`~ibkr_tracker.history.MIN_CAGR_YEARS` - annualising a few weeks is meaningless."""
    if h.empty:
        return pd.DataFrame()
    f = h.since(since)
    u = "base" if unit == "base" and h.base_currency != "USD" else "usd"
    ccy = h.base_currency if u == "base" else "USD"
    out = pd.DataFrame(index=f.index)
    if kind == "value":
        out[f"Portfolio value ({ccy})"] = f[f"total_{u}"]
        out[f"Net deposits ({ccy})"] = f[f"deposits_{u}"]
    elif kind == "gain":
        out[f"Gain ({ccy})"] = f[f"gain_{u}"]
    elif kind == "return":
        out[f"Return ({ccy}) - value / net deposits"] = f[f"return_{u}"]
    elif kind == "cagr":
        out[f"CAGR ({ccy})"] = f[f"cagr_{u}"].where(f["years"] >= MIN_CAGR_YEARS)
    elif kind == "benchmark":
        sym = h.benchmark_symbol or "Benchmark"
        out["Stocks (USD)"] = f["stocks_usd"]
        if h.benchmark_available:
            out[f"{sym} replay (USD)"] = f["bench_value_usd"]                   # (short: the legend must fit)
    elif kind == "benchmark_cagr":
        sym = h.benchmark_symbol or "Benchmark"
        young = f["bench_years"] < MIN_CAGR_YEARS
        out["Stocks CAGR (USD)"] = f["cagr_stocks"].where(~young)
        if h.benchmark_available:
            out[f"{sym} CAGR (USD)"] = f["cagr_bench"].where(~young)
    elif kind == "allocation":
        scale = f["base_per_usd"] if u == "base" else 1.0
        pos = h.positions.loc[f.index]
        for name in pos.columns:
            out[name] = pos[name] * scale
        out["Cash"] = f["cash_usd"] * scale
    else:
        raise ValueError(f"unknown chart kind {kind!r}")
    return out


def coverage_table(h: History) -> pd.DataFrame:
    """How well the price data cover the days every stock was held (the *Market data* tab and the dashboard).
    The status - the column that matters, and the longest text - comes right after the ticker; dates are text so an
    empty one is blank instead of a grey "None"."""
    day = lambda d: d.isoformat() if d else ""                                       # noqa: E731
    return pd.DataFrame([{
        "Ticker": c.ticker, "Currency": c.currency, "Status": c.status, "Days held": c.held_days, "Prices": c.points,
        "Yahoo": c.yahoo_points, "Manual": c.manual_points, "Statement": c.statement_points,
        "First price": day(c.first_price), "Last price": day(c.last_price)} for c in h.coverage])


# ---------------------------------------------------------------------------------------------------------------
# ticker mapping / manual price history (Market data tab)
# ---------------------------------------------------------------------------------------------------------------
def mapping_table(rows) -> pd.DataFrame:
    """The ticker-mapping editor: IBKR ticker, the automatic Yahoo symbol, the user's alias and the download status.
    The columns that matter come first - the wide name and the exchange (the source of the automatic symbol) last."""
    return pd.DataFrame([{
        "Ticker": r.ticker, "Currency": r.currency, "Held now": r.held, "Automatic symbol": r.auto_symbol,
        "Your symbol (alias)": r.alias, "Manual prices only": r.manual_only,
        "Symbol used": "-" if r.manual_only else r.symbol, "Status": ("[!] " if r.problem else "") + r.status,
        "Name": r.name, "IBKR exchange": r.exchange} for r in rows])


def manual_prices_table(portfolio: Portfolio, summary: dict, benchmark_key: str, benchmark_symbol: str = "") -> pd.DataFrame:
    """Overview of the manual price history of every stock (and of the benchmark)."""
    held = portfolio.net_quantities()
    rows = []
    entries = [(f"{t}|{c}", t, c, "no" if is_closed(held.get((t, c), 0.0)) else "yes")
               for t, c in portfolio.positions_keys()]
    entries.append((benchmark_key, benchmark_symbol or "Benchmark", "benchmark", "-"))
    day = lambda d: d.isoformat() if d else ""                                       # noqa: E731  (text: no grey "None")
    for key, ticker, ccy, is_held in entries:
        info = summary.get(key) or {}
        rows.append({"Ticker": ticker, "Currency": ccy, "Held now": is_held, "Manual prices": info.get("count", 0),
                     "From": day(info.get("first")), "To": day(info.get("last")), "Source": info.get("source", ""),
                     "Entered": str(info.get("imported_at", "")).replace("T", " ")[:16]})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------------------------
# portfolio analysis: allocation (donut chart) and risk metrics
# ---------------------------------------------------------------------------------------------------------------
def allocation_table(a: Allocation) -> pd.DataFrame:
    """The slices of one grouping: USD value, weight (a fraction of the positive values) and the holdings in it."""
    label = GROUPINGS[a.by]
    return pd.DataFrame([{label: s.label, "USD Value": s.usd_value, "Weight": s.weight, "Holdings": len(s.members),
                          "Members": ", ".join(s.members)} for s in a.slices])


def allocation_long_table(allocations: list[Allocation]) -> pd.DataFrame:
    """Every grouping stacked (``allocation.csv``): ``Grouping, Group, USD Value, Weight, Holdings, Members``."""
    rows = []
    for a in allocations:
        for s in a.slices:
            rows.append({"Grouping": GROUPINGS[a.by], "Group": s.label, "USD Value": s.usd_value, "Weight": s.weight,
                         "Holdings": len(s.members), "Members": ", ".join(s.members)})
    return pd.DataFrame(rows)


def concentration_cards(c: Concentration) -> list[tuple[str, Optional[float], str]]:
    """``(label, value, kind)`` of the concentration figures - kind ``"pct"``, ``"count"`` or ``"number"``."""
    return [("Stock positions", float(c.n_positions), "count"),
            (f"Largest position{f' ({c.largest_ticker})' if c.largest_ticker else ''}", c.largest_weight, "pct"),
            ("Top 3 positions", c.top3_weight, "pct"), ("Top 5 positions", c.top5_weight, "pct"),
            ("Effective positions", c.effective_positions, "number")]


def classification_table(holdings: list[Holding]) -> pd.DataFrame:
    """The classification editor: the stocks with their asset class and sector (the two editable columns) and where
    each of them comes from.  Cash is not listed - it is always *Cash*."""
    total = sum(h.usd_value for h in holdings if h.usd_value > 0)
    return pd.DataFrame([{
        "Ticker": h.ticker, "Currency": h.currency, "Name": h.name, "Statement type": h.ibkr_type or "-",
        "USD Value": h.usd_value, "Weight": (h.usd_value / total) if total > 0 and h.usd_value > 0 else None,
        "Asset class": h.asset_class, "Sector": h.sector, "Sector from": h.sector_source}
        for h in holdings if not h.is_cash])


def holdings_table(holdings: list[Holding]) -> pd.DataFrame:
    """Every holding with its classification (``holdings.csv``)."""
    total = sum(h.usd_value for h in holdings if h.usd_value > 0)
    return pd.DataFrame([{
        "Holding": h.ticker, "Currency": h.currency, "Name": h.name, "Statement type": h.ibkr_type,
        "Asset class": h.asset_class, "Stock type": h.stock_type, "Sector": h.sector, "Industry": h.industry,
        "USD Value": h.usd_value, "Weight": (h.usd_value / total) if total > 0 and h.usd_value > 0 else None}
        for h in holdings])


def _fmt_metric(v, kind: str) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return "n/a"
    if kind == analytics.PCT:
        return f"{v * 100:.2f}%"
    if kind == analytics.RATIO:
        return f"{v:.2f}"
    if kind == analytics.NUMBER:
        return f"{v:,.2f}"
    return str(v) if v != "" else "n/a"


def risk_table(rep: analytics.RiskReport) -> pd.DataFrame:
    """The metrics table of the dashboard (formatted text): ``Group, Metric, Portfolio, Benchmark, What it means``."""
    bench = f"Benchmark ({rep.benchmark_symbol})" if rep.benchmark_symbol else "Benchmark"
    return pd.DataFrame([{
        "Group": r.group, "Metric": r.label, "Portfolio": _fmt_metric(r.portfolio, r.kind),
        bench: _fmt_metric(r.benchmark, r.kind), "What it means": r.help} for r in rep.rows()])


def risk_values_table(rep: analytics.RiskReport) -> pd.DataFrame:
    """The metrics as numbers (``risk_metrics.csv``): percentages are fractions (0.1234 = 12.34 %)."""
    fmt = {analytics.PCT: "percent (as a fraction)", analytics.RATIO: "ratio", analytics.NUMBER: "number",
           analytics.TEXT: "text"}
    return pd.DataFrame([{
        "Currency": rep.currency, "Group": r.group, "Metric": r.label, "Portfolio": r.portfolio,
        "Benchmark": r.benchmark, "Format": fmt.get(r.kind, r.kind), "Window start": rep.start, "Window end": rep.end,
        "Observation days": rep.n_obs, "Risk-free rate": rep.risk_free} for r in rep.rows()])


RISK_DAILY_COLUMNS = {
    "value": "Value", "flow": "Net deposits of the day", "ret": "Return", "index": "Wealth index", "peak": "Peak",
    "drawdown": "Drawdown", "rolling_vol": f"Rolling volatility ({analytics.ROLLING_WINDOW} days)",
    "bench_price": "Benchmark price", "bench_ret": "Benchmark return", "bench_index": "Benchmark wealth index",
    "bench_drawdown": "Benchmark drawdown"}


def risk_daily_table(rep: analytics.RiskReport) -> pd.DataFrame:
    """One row per observation day (``risk_daily.csv``) - the numbers behind the drawdown and volatility charts."""
    if rep.frame.empty:
        return pd.DataFrame()
    out = rep.frame[list(RISK_DAILY_COLUMNS)].rename(columns=RISK_DAILY_COLUMNS).copy()
    out.index = [t.date().isoformat() for t in out.index]
    out.index.name = "Date"
    out.insert(0, "Currency", rep.currency)
    return out.reset_index()


def risk_chart_frame(rep: analytics.RiskReport, kind: str) -> pd.DataFrame:
    """Chart data of a report: ``kind`` = ``"drawdown"`` | ``"rolling_vol"`` (percent, portfolio and benchmark columns)."""
    f = rep.frame
    if f.empty:
        return pd.DataFrame()
    bench = (rep.benchmark_symbol or "Benchmark").split(".")[0]
    if kind == "drawdown":
        cols = {"drawdown": "Portfolio", "bench_drawdown": bench}
        data = f[[c for c in cols if c in f]].rename(columns=cols) * 100.0
    elif kind == "rolling_vol":
        data = pd.DataFrame({"Portfolio": f["rolling_vol"] * 100.0})
        rb = f["bench_ret"].rolling(analytics.ROLLING_WINDOW, min_periods=analytics.ROLLING_WINDOW).std()
        if rb.notna().any():
            data[bench] = rb * (analytics.TRADING_DAYS ** 0.5) * 100.0
    else:
        raise ValueError(f"unknown chart {kind!r}")
    data.index.name = "Date"
    return data.dropna(axis=1, how="all")
