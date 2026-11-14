"""pandas views of the results (for the GUI).  Column names mirror the spreadsheet headers."""
from __future__ import annotations

from typing import Optional

import pandas as pd

from .calc import Metrics
from .portfolio import Portfolio
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


def positions_table(m: Metrics) -> pd.DataFrame:
    """Section 6.  ``Quantity`` is the share count of the latest statement's Open Positions; the last two columns
    show the sum of the transactions and the resulting factor (differs from 1 after a stock split, a transfer...)."""
    return pd.DataFrame([{
        "Ticker": r.ticker, "Name": r.name, "Currency": r.currency, "Quantity": r.quantity,
        "Current Price": r.price, "Value": r.value, "Return": r.ret, "USD Value": r.usd_value,
        "USD Return": r.usd_ret, "CAGR": None, "Quantity (transactions)": r.tx_quantity,
        "Share factor": r.share_factor} for r in m.positions])


def cash_table(m: Metrics) -> pd.DataFrame:
    return pd.DataFrame([{"Currency": c.currency, "Net cash": c.balance, "USD Value": c.usd_value} for c in m.cash])


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
    for ticker, ccy in portfolio.positions_keys():
        key = f"{ticker}|{ccy}"
        raw = md.raw_prices.get(key)
        used = md.prices.get(key)
        rows.append({
            "Ticker": ticker, "Currency": ccy, "Yahoo symbol": md.yahoo_symbols.get(key, ticker),
            "Last close": raw.price if raw else None, "Price date": raw.day if raw else None,
            "Source": raw.source if raw else "MISSING - refresh or enter an override",
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
            "Source": raw.source if raw else ("MISSING - refresh or enter an override" if ccy in active
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


def summary_table(m: Metrics, p: Portfolio) -> pd.DataFrame:
    """Sections 8-10 as plain rows (Excel / CSV export of the headline figures)."""
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
    return pd.DataFrame(rows, columns=["Item", "Value", "Unit"])
