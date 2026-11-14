"""Turn a :class:`RawStatement` (CSV or HTML) into the typed :class:`ParsedStatement` model."""
from __future__ import annotations

from collections import Counter
from typing import Any

from ..models import (Accrual, CashFlow, CashLine, DividendLine, FxTrade, Instrument, OpenPosition,
                      ParsedStatement, StockTrade)
from .raw import RawStatement
from .util import (description_key, is_dividend_related, num0, parse_date, parse_dt, parse_period, pick,
                   split_description, to_num)

HANDLED_SECTIONS = {
    "Trades", "Deposits & Withdrawals", "Fees", "Dividends", "Withholding Tax", "Interest",
    "Change in Dividend Accruals", "Financial Instrument Information", "Open Positions", "Cash Report",
    "Net Asset Value", "Mark-to-Market Performance Summary",
    # splits, mergers, spin-offs ...: deliberately NOT used - the Open Positions section is the source of truth for
    # the share counts (see ibkr_tracker.portfolio); only a note is written
    "Corporate Actions",
}
# purely informational report sections: they never change cash or positions
INFORMATIONAL_SECTIONS = {
    "Realized & Unrealized Performance Summary", "Codes", "Notes/Legal Notes", "Change in NAV",
    "Statement", "Account Information", "Change in Interest Accruals", "Interest Accruals",
    "Total P/L for Statement Period", "Cash Report Summary", "Base Currency Exchange Rate",
    "Forex Balances", "Net Stock Position Summary",
}


def _cost(x: Any) -> float:
    """IBKR reports commissions/fees/taxes as negative numbers -> return a positive cost (0.0 for blanks)."""
    n = to_num(x)
    return 0.0 if n is None else 0.0 - n


def _is_total_row(*values: Any) -> bool:
    return any(str(v).strip().lower().startswith("total") for v in values if v is not None)


def build_statement(raw: RawStatement, filename: str, sha256: str) -> ParsedStatement:
    meta = raw.meta
    start, end = parse_period(meta.get("period", ""))
    ps = ParsedStatement(
        filename=filename, fmt=raw.fmt, sha256=sha256,
        account=meta.get("account", "").strip(), alias=meta.get("alias", "").strip(),
        holder=meta.get("holder", "").strip(), base_currency=meta.get("base_currency", "").strip().upper(),
        period_start=start, period_end=end, generated=meta.get("generated", ""), precision=raw.precision,
    )
    prec = raw.precision
    sec = raw.sections
    unsupported: Counter[str] = Counter()

    if not ps.account:
        ps.warnings.append("Account id not found in the statement header.")
    if not ps.base_currency:
        ps.warnings.append("Base currency not found in the statement header.")
    if start is None or end is None:
        ps.warnings.append(f"Statement period could not be parsed ({meta.get('period', '')!r}).")

    # ---- instruments -------------------------------------------------------------------------------------
    for r in sec.get("Financial Instrument Information", []):
        sym = pick(r, "Symbol").strip()
        if not sym:
            continue
        ps.instruments.append(Instrument(
            ticker=sym, name=pick(r, "Description") or sym, asset_class=pick(r, "Asset Category", default="Stocks"),
            conid=str(pick(r, "Conid")), isin=pick(r, "Security ID"), exchange=pick(r, "Listing Exch"),
            type=pick(r, "Type"), multiplier=to_num(pick(r, "Multiplier")) or 1.0,
        ))
    names = {i.ticker: i.name for i in ps.instruments}

    # ---- trades (stocks + forex) -------------------------------------------------------------------------
    for r in sec.get("Trades", []):
        disc = pick(r, "DataDiscriminator", default="Order").strip().lower()
        if disc not in ("order", "trade"):          # ClosedLot / Lot rows only describe lots of a sale
            continue
        asset = pick(r, "Asset Category").strip()
        symbol = pick(r, "Symbol").strip()
        dt = parse_dt(pick(r, "Date/Time"))
        if not symbol or dt is None:
            continue
        qty = to_num(pick(r, "Quantity"))
        price = to_num(pick(r, "T. Price"))
        proceeds = to_num(pick(r, "Proceeds"))
        if qty is None or price is None:
            ps.warnings.append(f"Skipped unparsable trade row: {symbol} {dt}")
            continue
        proceeds = 0.0 - qty * price if proceeds is None else proceeds
        codes = pick(r, "Code")
        ccy = pick(r, "Currency").strip().upper()

        if asset == "Stocks":
            fee = _cost(pick(r, "Comm/Fee", "Comm", default=None))
            ps.trades.append(StockTrade(
                ticker=symbol, name=names.get(symbol, symbol), quantity=qty, price=price, proceeds=proceeds,
                fee=fee, currency=ccy, dt=dt, asset_class=asset, codes=codes, precision=prec))
            if abs(qty * price + proceeds) > 0.011 * max(1.0, abs(qty)):
                ps.warnings.append(
                    f"{symbol} {dt:%Y-%m-%d %H:%M:%S}: quantity x price ({qty * price:,.4f}) differs from the "
                    f"reported proceeds ({-proceeds:,.4f}); the spreadsheet uses quantity x price.")
        elif asset == "Forex":
            if "." not in symbol:
                unsupported["Forex (unrecognised symbol)"] += 1
                continue
            bought, paid = (p.strip().upper() for p in symbol.split(".", 1))
            comm_key = next((k for k in r if k.lower().startswith("comm")), None)
            fee_ccy = paid
            if comm_key and " in " in comm_key.lower():
                fee_ccy = comm_key.lower().split(" in ", 1)[1].strip().upper()
            fee = _cost(r.get(comm_key)) if comm_key else 0.0
            ps.fx_trades.append(FxTrade(
                bought=bought, paid=paid, quantity=qty, price=price, proceeds=proceeds, fee=fee,
                fee_currency=fee_ccy, dt=dt, codes=codes, precision=prec))
        else:
            unsupported[f"Trades: {asset or 'unknown asset class'}"] += 1

    # ---- deposits / withdrawals / fees / interest -------------------------------------------------------
    for r in sec.get("Deposits & Withdrawals", []):
        ccy = pick(r, "Currency").strip().upper()
        if not ccy or _is_total_row(ccy, pick(r, "Description")):
            continue
        dt = parse_dt(pick(r, "Date", "Settle Date"))
        amount = to_num(pick(r, "Amount"))
        if dt is None or amount is None:
            continue
        ps.cash_flows.append(CashFlow(
            kind="deposit" if amount >= 0 else "withdrawal", amount=amount, currency=ccy, dt=dt,
            description=pick(r, "Description"), precision=prec))

    for r in sec.get("Fees", []):
        ccy = pick(r, "Currency").strip().upper()
        if not ccy or _is_total_row(ccy, pick(r, "Subtitle"), pick(r, "Description")):
            continue
        dt = parse_dt(pick(r, "Date"))
        amount = to_num(pick(r, "Amount"))
        desc = pick(r, "Description")
        if dt is None or amount is None:
            continue
        if is_dividend_related(desc):               # e.g. ADR fee booked against a dividend payment
            ticker, isin, _ = split_description(desc)
            ps.div_lines.append(DividendLine(
                kind="fee", ticker=ticker, isin=isin, amount=amount, currency=ccy, dt=dt.date(),
                description=desc, key=description_key(desc), precision=prec))
        else:
            ps.cash_flows.append(CashFlow(
                kind="monthly fee", amount=amount, currency=ccy, dt=dt, description=desc or pick(r, "Subtitle"),
                precision=prec))

    for r in sec.get("Interest", []):
        ccy = pick(r, "Currency").strip().upper()
        if not ccy or _is_total_row(ccy, pick(r, "Description")):
            continue
        dt = parse_dt(pick(r, "Date"))
        amount = to_num(pick(r, "Amount"))
        if dt is None or amount is None:
            continue
        ps.cash_flows.append(CashFlow(kind="interest", amount=amount, currency=ccy, dt=dt,
                                      description=pick(r, "Description"), precision=prec))

    # ---- dividends & withholding tax --------------------------------------------------------------------
    for section, kind in (("Dividends", "dividend"), ("Withholding Tax", "tax")):
        for r in sec.get(section, []):
            ccy = pick(r, "Currency").strip().upper()
            if not ccy or _is_total_row(ccy, pick(r, "Description")):
                continue
            d = parse_date(pick(r, "Date"))
            amount = to_num(pick(r, "Amount"))
            desc = pick(r, "Description")
            if d is None or amount is None:
                continue
            ticker, isin, _ = split_description(desc)
            if not ticker:
                ticker = desc.split()[0] if desc.split() else "?"
            ps.div_lines.append(DividendLine(
                kind=kind, ticker=ticker, isin=isin, amount=amount, currency=ccy, dt=d, description=desc,
                key=description_key(desc), precision=prec))

    for r in sec.get("Change in Dividend Accruals", []):
        sym = pick(r, "Symbol").strip()
        if not sym or pick(r, "Asset Category", default="Stocks") != "Stocks":
            continue
        d = parse_date(pick(r, "Date"))
        if d is None:
            continue
        ps.accruals.append(Accrual(
            ticker=sym, currency=pick(r, "Currency").strip().upper(), dt=d,
            ex_date=parse_date(pick(r, "Ex Date")), pay_date=parse_date(pick(r, "Pay Date")),
            quantity=num0(pick(r, "Quantity")), gross_rate=num0(pick(r, "Gross Rate")),
            gross_amount=num0(pick(r, "Gross Amount")), net_amount=num0(pick(r, "Net Amount")),
            code=pick(r, "Code")))

    # ---- Open Positions: the source of truth for the share counts (also: reconciliation, offline pricing) --------
    ps.open_positions_section = bool(sec.get("Open Positions"))
    for r in sec.get("Open Positions", []):
        if pick(r, "DataDiscriminator", default="Summary").strip().lower() != "summary":
            continue
        sym = pick(r, "Symbol").strip()
        if not sym or pick(r, "Asset Category") != "Stocks":
            continue
        ps.open_positions.append(OpenPosition(
            ticker=sym, currency=pick(r, "Currency").strip().upper(), quantity=num0(pick(r, "Quantity")),
            cost_basis=num0(pick(r, "Cost Basis")), close_price=num0(pick(r, "Close Price")),
            value=num0(pick(r, "Value")), asset_class="Stocks"))

    for r in sec.get("Cash Report", []):
        line = pick(r, "Currency Summary").strip()
        ccy = pick(r, "Currency").strip()
        total = to_num(pick(r, "Total"))
        if line and ccy and total is not None:
            ps.cash_report.append(CashLine(currency=ccy.upper() if len(ccy) == 3 else ccy, line=line, total=total))

    for r in sec.get("Net Asset Value", []):
        if "Time Weighted Rate of Return" in r:
            ps.twr_pct = to_num(r["Time Weighted Rate of Return"])
            continue
        label = pick(r, "Asset Class").strip().lower()
        val = to_num(pick(r, "Current Total"))
        if label and val is not None:
            ps.nav[label] = val

    for r in sec.get("Mark-to-Market Performance Summary", []):
        if pick(r, "Asset Category") == "Forex":
            cur = to_num(pick(r, "Current Price"))
            sym = pick(r, "Symbol").strip().upper()
            if cur and sym:
                ps.fx_closes[sym] = cur

    n_actions = sum(1 for r in sec.get("Corporate Actions", []) if not _is_total_row(*r.values()))
    if n_actions:
        ps.notes.append(
            f"{n_actions} corporate-action row(s) (stock split, merger, spin-off ...) are not used: the share counts "
            f"come from the Open Positions section. Cash paid by a corporate action (e.g. cash in lieu of fractional "
            f"shares) is not part of the computed cash balance - the cash check on the Reconciliation tab shows it.")

    # ---- names, unsupported sections ---------------------------------------------------------------------
    for name, rows in sec.items():
        if rows and name not in HANDLED_SECTIONS and name not in INFORMATIONAL_SECTIONS:
            unsupported[name] += len(rows)
    ps.unsupported = dict(unsupported)
    for k, n in unsupported.items():
        ps.warnings.append(
            f"{n} row(s) in '{k}' are not supported and were ignored - cash balances / positions may differ "
            f"from IBKR (see the Reconciliation tab).")
    return ps
