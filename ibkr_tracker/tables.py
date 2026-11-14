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


def trades_table(m: Metrics) -> pd.DataFrame:
    rows = []
    for r in m.trades:
        t = r.trade
        rows.append({
            "Name": t.name, "Ticker": t.ticker, "Quantity": t.quantity, "Price": t.price, "Total": r.total,
            "Fee": t.fee, "Currency": t.currency, "Date": t.dt, "Gross": r.gross, "Current Price": r.current_price,
            "Current Total": r.current_total, "USD Total": r.usd_total, "USD Gross": r.usd_gross,
            "USD Current Total": r.usd_current, "VUAA Quantity": r.vuaa_qty})
    return pd.DataFrame(rows)


def dividends_table(p: Portfolio) -> pd.DataFrame:
    return pd.DataFrame([{
        "Name": d.name, "Ticker": d.ticker, "Quantity": d.quantity, "Dividend": d.per_share, "Total": d.total,
        "Withholding Tax": d.tax_fee, "Currency": d.currency, "Date": d.dt} for d in p.dividends])


def deposits_table(m: Metrics) -> pd.DataFrame:
    base = m.base_currency
    return pd.DataFrame([{
        "Description": d.kind, "Amount": d.amount, f"USD.{base}": d.rate, "USD Amount": d.usd_amount, "Date": d.day,
        "Currency": d.currency, f"Amount ({base})": d.base_amount} for d in m.deposits])


def forex_table(p: Portfolio) -> pd.DataFrame:
    return pd.DataFrame([{
        "Currency Bought": f.bought, "Quantity": f.quantity, "Price": f.price, "Total": f.quantity * f.price,
        "Fee": f.fee, "Currency Paid": f.paid, "Date": f.dt, "Fee Currency": f.fee_currency} for f in p.fx_trades])


def positions_table(m: Metrics) -> pd.DataFrame:
    return pd.DataFrame([{
        "Ticker": r.ticker, "Name": r.name, "Currency": r.currency, "Quantity": r.quantity,
        "Current Price": r.price, "Value": r.value, "Return": r.ret, "USD Value": r.usd_value,
        "USD Return": r.usd_ret, "CAGR": None} for r in m.positions])


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
