"""Synthetic IBKR Activity Statement (CSV) with hand-computable truth.

The sample statement shipped with the project contains only buys, one stock currency and a HUF base currency.
This generator exercises everything else: sells (negative quantity), a withdrawal, a monthly fee, interest, a
EUR-listed stock, a EUR base currency, forex trades with a base-currency commission and dividends with
withholding tax and an ADR-style fee.  ``truth`` is computed from the ledger below - NOT by the application.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime

ACCOUNT = "U9990001"
BASE = "EUR"
EUR_PER_USD_CLOSE = 0.9524          # period-end closing rate printed by the statement (1 USD = 0.9524 EUR)
USD_PER_EUR_TRADE = 1.03            # the forex trade of 2025-01-13
CLOSES = {"AAPL": 160.0, "SAP": 130.0}


@dataclass
class Truth:
    cash: dict[str, float] = field(default_factory=dict)
    positions: dict[tuple[str, str], float] = field(default_factory=dict)    # sum of the transactions that were kept
    holdings: dict[tuple[str, str], float] = field(default_factory=dict)     # what the statement's Open Positions show
    gross: dict[tuple[str, str], float] = field(default_factory=dict)        # sum(total + fee) per position
    deposits_eur: float = 0.0                                                # deposits + withdrawal + fee (no interest)
    n_stock_trades: int = 3
    n_fx_trades: int = 1
    n_cash_flows: int = 6
    n_dividends: int = 3
    closed: tuple = ()                                                       # fully closed positions (closed_position=True)
    dust: dict[str, float] = field(default_factory=dict)                    # sub-0.5 balances (dust_currency=True)


def _row(*cells) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(cells)
    return buf.getvalue()


GBP_PER_EUR_CLOSE = 1.17                  # period-end closing rate of the dust currency (1 GBP = 1.17 EUR)


def make_csv(period_start: date = date(2025, 1, 1), period_end: date = date(2025, 12, 31),
             only_before: date | None = None, only_from: date | None = None,
             split: tuple[str, float, date] | None = None, open_positions: bool = True,
             closed_position: bool = False, dust_currency: bool = False) -> tuple[bytes, Truth]:
    """Statement text + ground truth.  ``only_before`` / ``only_from`` keep just the transactions dated before /
    from that day (to build overlapping or consecutive statements out of one ledger).  ``truth.cash`` / ``positions``
    describe the transactions that were kept.

    Like a real statement the Open Positions section shows what the account HOLDS at ``period_end`` - all trades up
    to that day, whichever of them the file lists - and a stock split ``(symbol, ratio, day)`` multiplies the shares
    (and divides the close price) from ``day`` on WITHOUT touching the trades, exactly the situation in which the
    sum of the transactions no longer equals the share count.  The split also adds a "Corporate Actions" section.
    ``open_positions=False`` leaves the section out (a custom statement).

    ``closed_position=True`` adds TSLA (USD): bought 2, sold 2 - a position that is fully closed, so it is in the
    transaction history but not in the Open Positions.  ``dust_currency=True`` adds two GBP forex trades that leave
    0.45 GBP behind (a residual balance below 0.5) - both for the "hide closed positions and dust balances" filter."""
    def keep(d: date) -> bool:
        return (only_before is None or d < only_before) and (only_from is None or d >= only_from)

    L: list[str] = []
    add = L.append
    period = f"{period_start:%B} {period_start.day}, {period_start.year} - {period_end:%B} {period_end.day}, {period_end.year}"
    add("﻿" + _row("Statement", "Header", "Field Name", "Field Value"))
    add(_row("Statement", "Data", "BrokerName", "Interactive Brokers Ireland Limited"))
    add(_row("Statement", "Data", "Title", "Activity Statement"))
    add(_row("Statement", "Data", "Period", period))
    add(_row("Statement", "Data", "WhenGenerated", "2026-01-03, 10:00:00 EST"))
    add(_row("Account Information", "Header", "Field Name", "Field Value"))
    add(_row("Account Information", "Data", "Name", "Test Holder"))
    add(_row("Account Information", "Data", "Account Alias", "SYNTH"))
    add(_row("Account Information", "Data", "Account", ACCOUNT))
    add(_row("Account Information", "Data", "Base Currency", BASE))

    # ---- ledger ----------------------------------------------------------------------------------------------
    t = Truth()
    cash = {"EUR": 0.0, "USD": 0.0}
    if dust_currency:
        cash["GBP"] = 0.0

    deposits = [("EUR", date(2025, 1, 10), 10000.0, "Wire Transfer In (Initial funding)"),
                ("EUR", date(2025, 3, 5), 5000.0, "Wire Transfer In"),
                ("EUR", date(2025, 9, 1), -2000.0, "Disbursement Initiated by Test Holder")]
    fees = [("EUR", date(2025, 2, 3), -1.5, "Market Data (NASDAQ Network C/UTP) - Jan 2025")]
    interest = [("EUR", date(2025, 3, 4), 0.12, "EUR Credit Interest for Feb-2025")]
    fx = [("EUR", "USD", datetime(2025, 1, 13, 10, 0, 0), -3000.0, USD_PER_EUR_TRADE, -2.0)]  # sell 3000 EUR
    stocks = [("AAPL", "USD", datetime(2025, 1, 14, 15, 30, 0), 10.0, 150.0, -1.0),
              ("SAP", "EUR", datetime(2025, 1, 15, 9, 15, 0), 5.0, 120.0, -1.0),
              ("AAPL", "USD", datetime(2025, 6, 10, 15, 31, 0), -4.0, 170.0, -1.0)]
    dividends = [("AAPL", "US0378331005", "USD", date(2025, 2, 13), 0.25, 2.50, -0.38, "US Tax", 0.0),
                 ("SAP", "DE0007164600", "EUR", date(2025, 5, 20), 2.35, 11.75, -3.10, "DE Tax", 0.0),
                 ("AAPL", "US0378331005", "USD", date(2025, 8, 14), 0.25, 1.50, -0.23, "US Tax", -0.05)]

    if closed_position:
        stocks += [("TSLA", "USD", datetime(2025, 2, 20, 16, 0, 0), 2.0, 200.0, -1.0),
                   ("TSLA", "USD", datetime(2025, 5, 6, 16, 0, 0), -2.0, 230.0, -1.0)]
        t.closed = (("TSLA", "USD"),)
    if dust_currency:
        fx += [("GBP", "USD", datetime(2025, 4, 2, 10, 0, 0), 100.30, 1.27, -2.0),       # buy 100.30 GBP with USD
               ("GBP", "USD", datetime(2025, 4, 3, 10, 0, 0), -99.85, 1.28, -2.0)]       # sell 99.85 GBP: 0.45 left

    for ccy, d, amt, _ in deposits:
        if keep(d):
            cash[ccy] += amt
            t.deposits_eur += amt
    for ccy, d, amt, _ in fees:
        if keep(d):
            cash[ccy] += amt
            t.deposits_eur += amt
    for ccy, d, amt, _ in interest:
        if keep(d):
            cash[ccy] += amt
    for bought, paid, dt, qty, price, comm in fx:
        if keep(dt.date()):
            cash[bought] += qty
            cash[paid] -= qty * price
            cash[BASE] += comm
    pos: dict[tuple[str, str], float] = {}
    for sym, ccy, dt, qty, price, comm in stocks:
        if keep(dt.date()):
            cash[ccy] += -qty * price + comm
            pos[(sym, ccy)] = pos.get((sym, ccy), 0.0) + qty
            t.gross[(sym, ccy)] = t.gross.get((sym, ccy), 0.0) + qty * price - comm
    for sym, isin, ccy, d, per, tot, tax, tax_kind, fee in dividends:
        if keep(d):
            cash[ccy] += tot + tax + fee
    t.cash = {k: round(v, 10) for k, v in cash.items()}
    if dust_currency:
        t.dust = {"GBP": t.cash["GBP"]}
    t.positions = pos
    n_st = sum(1 for s in stocks if keep(s[2].date()))
    t.n_stock_trades, t.n_fx_trades = n_st, sum(1 for f in fx if keep(f[2].date()))
    t.n_cash_flows = (sum(1 for x in deposits if keep(x[1])) + sum(1 for x in fees if keep(x[1]))
                      + sum(1 for x in interest if keep(x[1])))
    t.n_dividends = sum(1 for x in dividends if keep(x[3]))

    # ---- cash report / NAV ------------------------------------------------------------------------------------
    add(_row("Cash Report", "Header", "Currency Summary", "Currency", "Total", "Securities", "Futures", ""))
    for ccy in ("EUR", "USD", *(("GBP",) if dust_currency else ())):
        add(_row("Cash Report", "Data", "Starting Cash", ccy, 0, 0, 0, ""))
        add(_row("Cash Report", "Data", "Ending Cash", ccy, repr(t.cash[ccy]), repr(t.cash[ccy]), 0, ""))
    add(_row("Cash Report", "Data", "Ending Cash", "Base Currency Summary", repr(t.cash["EUR"]), repr(t.cash["EUR"]), 0, ""))

    # ---- period-end closing FX (Mark-to-Market Forex rows: price of 1 unit in base currency, 4 decimals) -------------
    add(_row("Mark-to-Market Performance Summary", "Header", "Asset Category", "Symbol", "Prior Quantity",
             "Current Quantity", "Prior Price", "Current Price", "Mark-to-Market P/L Position",
             "Mark-to-Market P/L Transaction", "Mark-to-Market P/L Commissions", "Mark-to-Market P/L Other",
             "Mark-to-Market P/L Total", "Code"))
    add(_row("Mark-to-Market Performance Summary", "Data", "Forex", "EUR", 0, t.cash["EUR"], "1.0000", "1.0000",
             0, 0, 0, 0, 0, ""))
    add(_row("Mark-to-Market Performance Summary", "Data", "Forex", "USD", 0, t.cash["USD"], "--",
             f"{EUR_PER_USD_CLOSE:.4f}", 0, 0, 0, 0, 0, ""))
    if dust_currency:
        add(_row("Mark-to-Market Performance Summary", "Data", "Forex", "GBP", 0, t.cash["GBP"], "--",
                 f"{GBP_PER_EUR_CLOSE:.4f}", 0, 0, 0, 0, 0, ""))

    # ---- open positions: what is held at the end of the period -----------------------------------------------------
    held: dict[tuple[str, str], float] = {}
    for sym, ccy, dt, qty, price, comm in stocks:
        if dt.date() <= period_end:
            held[(sym, ccy)] = held.get((sym, ccy), 0.0) + qty
    closes = dict(CLOSES)
    if split and split[2] <= period_end:
        for key in held:
            if key[0] == split[0]:
                held[key] *= split[1]
        closes[split[0]] = CLOSES[split[0]] / split[1]
    held = {k: v for k, v in held.items() if abs(v) > 1e-9}
    t.holdings = dict(held)
    if open_positions:
        add(_row("Open Positions", "Header", "DataDiscriminator", "Asset Category", "Currency", "Symbol", "Quantity",
                 "Mult", "Cost Price", "Cost Basis", "Close Price", "Value", "Unrealized P/L", "Code"))
        for (sym, ccy), q in sorted(held.items()):
            add(_row("Open Positions", "Data", "Summary", "Stocks", ccy, sym, q, 1, 100, 100 * q, closes[sym],
                     closes[sym] * q, 0, ""))
    if split and split[2] <= period_end:
        sym, ratio, day = split
        before = sum(q for s_, c_, dt, q, _p, _c in stocks if s_ == sym and dt.date() <= day)
        add(_row("Corporate Actions", "Header", "Asset Category", "Currency", "Report Date", "Date/Time", "Description",
                 "Quantity", "Proceeds", "Value", "Realized P/L", "Code"))
        add(_row("Corporate Actions", "Data", "Stocks", "USD", day.isoformat(), f"{day.isoformat()}, 20:25:00",
                 f"{sym}(US0378331005) Split {ratio:g} for 1 ({sym}, APPLE INC, US0378331005)",
                 before * (ratio - 1), 0, 0, 0, ""))
        add(_row("Corporate Actions", "Total", "", "", "", "", "", "", 0, 0, 0, ""))

    # ---- trades --------------------------------------------------------------------------------------------------
    add(_row("Trades", "Header", "DataDiscriminator", "Asset Category", "Currency", "Symbol", "Date/Time", "Quantity",
             "T. Price", "C. Price", "Proceeds", "Comm/Fee", "Basis", "Realized P/L", "MTM P/L", "Code"))
    for sym, ccy, dt, qty, price, comm in stocks:
        if not keep(dt.date()):
            continue
        add(_row("Trades", "Data", "Order", "Stocks", ccy, sym, f"{dt:%Y-%m-%d}, {dt:%H:%M:%S}", qty, price, price,
                 -qty * price, comm, qty * price - comm, 0, 0, "O" if qty > 0 else "C"))
        if qty < 0:
            add(_row("Trades", "Data", "ClosedLot", "", ccy, sym, f"{dt:%Y-%m-%d}, 00:00:00", "", "", "", "", "", 0, 0, 0, ""))
    add(_row("Trades", "Total", "", "Stocks", "USD", "", "", "", "", "", 0, 0, 0, 0, 0, ""))
    add(_row("Trades", "Header", "DataDiscriminator", "Asset Category", "Currency", "Symbol", "Date/Time", "Quantity",
             "T. Price", "", "Proceeds", "Comm in EUR", "", "", "MTM in EUR", "Code"))
    for bought, paid, dt, qty, price, comm in fx:
        if keep(dt.date()):
            add(_row("Trades", "Data", "Order", "Forex", paid, f"{bought}.{paid}", f"{dt:%Y-%m-%d}, {dt:%H:%M:%S}", qty,
                     price, "", -qty * price, comm, "", "", 0, "AFx"))

    # ---- cash movements --------------------------------------------------------------------------------------
    add(_row("Deposits & Withdrawals", "Header", "Currency", "Settle Date", "Description", "Amount"))
    for ccy, d, amt, desc in deposits:
        if keep(d):
            add(_row("Deposits & Withdrawals", "Data", ccy, d.isoformat(), desc, amt))
    add(_row("Deposits & Withdrawals", "Data", "Total", "", "", 13000.0))
    add(_row("Fees", "Header", "Subtitle", "Currency", "Date", "Description", "Amount"))
    for ccy, d, amt, desc in fees:
        if keep(d):
            add(_row("Fees", "Data", "Other Fees", ccy, d.isoformat(), desc, amt))
    for sym, isin, ccy, d, per, tot, tax, tax_kind, fee in dividends:
        if keep(d) and fee:
            add(_row("Fees", "Data", "Other Fees", ccy, d.isoformat(),
                     f"{sym}({isin}) Cash Dividend {ccy} {per} per Share - FEE", fee))
    add(_row("Fees", "Data", "Total", "", "", "", -1.55))
    add(_row("Interest", "Header", "Currency", "Date", "Description", "Amount"))
    for ccy, d, amt, desc in interest:
        if keep(d):
            add(_row("Interest", "Data", ccy, d.isoformat(), desc, amt))
    add(_row("Interest", "Data", "Total", "", "", 0.12))
    add(_row("Dividends", "Header", "Currency", "Date", "Description", "Amount"))
    for sym, isin, ccy, d, per, tot, tax, tax_kind, fee in dividends:
        if keep(d):
            add(_row("Dividends", "Data", ccy, d.isoformat(),
                     f"{sym}({isin}) Cash Dividend {ccy} {per} per Share (Ordinary Dividend)", tot))
    add(_row("Dividends", "Data", "Total", "", "", 15.75))
    add(_row("Withholding Tax", "Header", "Currency", "Date", "Description", "Amount", "Code"))
    for sym, isin, ccy, d, per, tot, tax, tax_kind, fee in dividends:
        if keep(d):
            add(_row("Withholding Tax", "Data", ccy, d.isoformat(),
                     f"{sym}({isin}) Cash Dividend {ccy} {per} per Share - {tax_kind}", tax, ""))
    add(_row("Withholding Tax", "Data", "Total", "", "", -3.71, ""))
    add(_row("Financial Instrument Information", "Header", "Asset Category", "Symbol", "Description", "Conid",
             "Security ID", "Underlying", "Listing Exch", "Multiplier", "Type", "Code"))
    add(_row("Financial Instrument Information", "Data", "Stocks", "AAPL", "APPLE INC", 265598, "US0378331005", "AAPL",
             "NASDAQ", 1, "COMMON", ""))
    add(_row("Financial Instrument Information", "Data", "Stocks", "SAP", "SAP SE", 14204, "DE0007164600", "SAP",
             "IBIS", 1, "COMMON", ""))
    if closed_position:
        add(_row("Financial Instrument Information", "Data", "Stocks", "TSLA", "TESLA INC", 76792991, "US88160R1014",
                 "TSLA", "NASDAQ", 1, "COMMON", ""))
    return ("\n".join(L) + "\n").encode("utf-8"), t


# ======================================================================================================================
# A second, even simpler account: USD base currency, USD stocks only (no forex at all).  The common case for
# US-resident IBKR customers, and the one where "USD.<base>" degenerates to 1 and the FX sheet is empty.
# ======================================================================================================================
USD_ACCOUNT = "U9990002"
USD_CLOSES = {"MSFT": 500.0, "KO": 70.0}


@dataclass
class UsdTruth:
    cash: float = 0.0
    positions: dict[str, float] = field(default_factory=dict)
    gross: dict[str, float] = field(default_factory=dict)
    contributions: dict[date, float] = field(default_factory=dict)       # deposits + withdrawal (USD)
    stock_value: float = 0.0
    nav: float = 0.0


def make_usd_csv() -> tuple[bytes, UsdTruth]:
    L: list[str] = []
    add = L.append
    add("﻿" + _row("Statement", "Header", "Field Name", "Field Value"))
    add(_row("Statement", "Data", "BrokerName", "Interactive Brokers LLC"))
    add(_row("Statement", "Data", "Title", "Activity Statement"))
    add(_row("Statement", "Data", "Period", "January 1, 2025 - December 31, 2025"))
    add(_row("Statement", "Data", "WhenGenerated", "2026-01-03, 10:00:00 EST"))
    add(_row("Account Information", "Header", "Field Name", "Field Value"))
    add(_row("Account Information", "Data", "Name", "Usd Holder"))
    add(_row("Account Information", "Data", "Account", USD_ACCOUNT))
    add(_row("Account Information", "Data", "Base Currency", "USD"))

    deposits = [(date(2025, 2, 3), 5000.0, "Electronic Fund Transfer"),
                (date(2025, 6, 2), 3000.0, "Electronic Fund Transfer"),
                (date(2025, 10, 1), -1000.0, "Disbursement Initiated by Usd Holder")]
    stocks = [("MSFT", datetime(2025, 2, 10, 14, 35, 0), 10.0, 400.0, -1.0),
              ("KO", datetime(2025, 3, 12, 15, 0, 0), 20.0, 60.0, -1.0),
              ("MSFT", datetime(2025, 8, 12, 16, 10, 0), -4.0, 450.0, -1.0)]
    div = ("KO", "US1912161007", date(2025, 4, 15), 0.51, 10.20, -1.53)

    t = UsdTruth()
    cash = sum(a for _, a, _ in deposits)
    for d, a, _ in deposits:
        t.contributions[d] = a
    for sym, dt, q, px, comm in stocks:
        cash += -q * px + comm
        t.positions[sym] = t.positions.get(sym, 0.0) + q
        t.gross[sym] = t.gross.get(sym, 0.0) + q * px - comm
    cash += div[4] + div[5]
    t.cash = round(cash, 10)
    t.stock_value = sum(q * USD_CLOSES[s] for s, q in t.positions.items())
    t.nav = t.cash + t.stock_value

    add(_row("Net Asset Value", "Header", "Asset Class", "Prior Total", "Current Long", "Current Short", "Current Total", "Change"))
    add(_row("Net Asset Value", "Data", "Cash ", 0, repr(t.cash), 0, repr(t.cash), repr(t.cash)))
    add(_row("Net Asset Value", "Data", "Stock", 0, repr(t.stock_value), 0, repr(t.stock_value), repr(t.stock_value)))
    add(_row("Net Asset Value", "Data", "Total", 0, repr(t.nav), 0, repr(t.nav), repr(t.nav)))
    add(_row("Cash Report", "Header", "Currency Summary", "Currency", "Total", "Securities", "Futures", ""))
    add(_row("Cash Report", "Data", "Starting Cash", "Base Currency Summary", 0, 0, 0, ""))
    add(_row("Cash Report", "Data", "Ending Cash", "Base Currency Summary", repr(t.cash), repr(t.cash), 0, ""))
    add(_row("Cash Report", "Data", "Starting Cash", "USD", 0, 0, 0, ""))
    add(_row("Cash Report", "Data", "Ending Cash", "USD", repr(t.cash), repr(t.cash), 0, ""))
    add(_row("Open Positions", "Header", "DataDiscriminator", "Asset Category", "Currency", "Symbol", "Quantity", "Mult",
             "Cost Price", "Cost Basis", "Close Price", "Value", "Unrealized P/L", "Code"))
    for sym, q in sorted(t.positions.items()):
        add(_row("Open Positions", "Data", "Summary", "Stocks", "USD", sym, q, 1, 100, 100 * q, USD_CLOSES[sym],
                 USD_CLOSES[sym] * q, 0, ""))
    add(_row("Trades", "Header", "DataDiscriminator", "Asset Category", "Currency", "Symbol", "Date/Time", "Quantity",
             "T. Price", "C. Price", "Proceeds", "Comm/Fee", "Basis", "Realized P/L", "MTM P/L", "Code"))
    for sym, dt, q, px, comm in stocks:
        add(_row("Trades", "Data", "Order", "Stocks", "USD", sym, f"{dt:%Y-%m-%d}, {dt:%H:%M:%S}", q, px, px, -q * px,
                 comm, q * px - comm, 0, 0, "O" if q > 0 else "C"))
    add(_row("Trades", "Total", "", "Stocks", "USD", "", "", "", "", "", 0, 0, 0, 0, 0, ""))
    add(_row("Deposits & Withdrawals", "Header", "Currency", "Settle Date", "Description", "Amount"))
    for d, a, desc in deposits:
        add(_row("Deposits & Withdrawals", "Data", "USD", d.isoformat(), desc, a))
    add(_row("Deposits & Withdrawals", "Data", "Total", "", "", 7000.0))
    add(_row("Dividends", "Header", "Currency", "Date", "Description", "Amount"))
    add(_row("Dividends", "Data", "USD", div[2].isoformat(),
             f"{div[0]}({div[1]}) Cash Dividend USD {div[3]} per Share (Ordinary Dividend)", div[4]))
    add(_row("Dividends", "Data", "Total", "", "", 10.20))
    add(_row("Withholding Tax", "Header", "Currency", "Date", "Description", "Amount", "Code"))
    add(_row("Withholding Tax", "Data", "USD", div[2].isoformat(),
             f"{div[0]}({div[1]}) Cash Dividend USD {div[3]} per Share - US Tax", div[5], ""))
    add(_row("Withholding Tax", "Data", "Total", "", "", -1.53, ""))
    add(_row("Financial Instrument Information", "Header", "Asset Category", "Symbol", "Description", "Conid",
             "Security ID", "Underlying", "Listing Exch", "Multiplier", "Type", "Code"))
    add(_row("Financial Instrument Information", "Data", "Stocks", "MSFT", "MICROSOFT CORP", 272093, "US5949181045",
             "MSFT", "NASDAQ", 1, "COMMON", ""))
    add(_row("Financial Instrument Information", "Data", "Stocks", "KO", "COCA-COLA CO/THE", 4065, "US1912161007",
             "KO", "NYSE", 1, "COMMON", ""))
    return ("\n".join(L) + "\n").encode("utf-8"), t
