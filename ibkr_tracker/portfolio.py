"""Merge any number of parsed statements into one chronologically ordered :class:`Portfolio`.

Merge rules
-----------
* Statements of different accounts are never mixed (one account at a time).
* The same transaction that appears in several statements (overlapping periods, or the CSV *and* the HTML
  rendering of one statement) is kept **once**.  Matching is multiplicity aware: two identical fills inside
  one statement are two transactions, but "1 in file A + 1 in file B" is still one.
* When duplicates differ only by precision the full-precision record (CSV) wins over the rounded (HTML) one.
* Dividends, withholding taxes and dividend fees are synchronised by (ticker, date, dividend description).
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Hashable, Optional, Sequence, TypeVar

from .models import (Accrual, CashFlow, DividendEvent, DividendLine, FxTrade, Instrument, ParsedStatement,
                     StockTrade, _to_jsonable)
from .parsing.util import parse_rate

T = TypeVar("T")
AMOUNT_TOL = 0.0051          # IBKR's HTML rounds amounts to cents


# --------------------------------------------------------------------------------------
# generic multiset merge
# --------------------------------------------------------------------------------------
def merge_multiset(batches: Sequence[Sequence[T]], key_fn: Callable[[T], Hashable],
                   amount_fn: Optional[Callable[[T], float]] = None, tol: float = AMOUNT_TOL) -> tuple[list[T], int]:
    """Union of several batches (oldest statement first) without double counting.

    Returns ``(records, n_duplicates_dropped)``.
    """
    slots_by_key: dict[Hashable, list[list[tuple[int, T]]]] = {}
    order: list[Hashable] = []
    total_in = 0
    for bi, batch in enumerate(batches):
        for rec in batch:
            total_in += 1
            k = key_fn(rec)
            if k not in slots_by_key:
                slots_by_key[k] = []
                order.append(k)
            amount = amount_fn(rec) if amount_fn else 0.0
            for slot in slots_by_key[k]:
                if any(b == bi for b, _ in slot):
                    continue                      # this batch already contributed its record to the slot
                ref = amount_fn(slot[0][1]) if amount_fn else 0.0
                if abs(ref - amount) <= tol:
                    slot.append((bi, rec))
                    break
            else:
                slots_by_key[k].append([(bi, rec)])
    out: list[T] = []
    for k in order:
        for slot in slots_by_key[k]:
            best = max(slot, key=lambda br: (getattr(br[1], "precision", "full") == "full", br[0]))
            out.append(best[1])
    return out, total_in - len(out)


# --------------------------------------------------------------------------------------
# portfolio container
# --------------------------------------------------------------------------------------
@dataclass
class Portfolio:
    account: str
    alias: str
    holder: str
    base_currency: str
    period_start: Optional[date]
    period_end: Optional[date]
    trades: list[StockTrade]
    fx_trades: list[FxTrade]
    cash_flows: list[CashFlow]
    dividends: list[DividendEvent]
    instruments: dict[str, Instrument]
    statements: list[ParsedStatement]
    latest: Optional[ParsedStatement]
    warnings: list[str] = field(default_factory=list)
    duplicates_dropped: int = 0

    # ---- derived lists ---------------------------------------------------------------------------------
    def positions_keys(self) -> list[tuple[str, str]]:
        """Unique (ticker, currency) of every stock ever bought, alphabetical (Section 6 rows)."""
        return sorted({(t.ticker, t.currency) for t in self.trades})

    def active_currencies(self) -> list[str]:
        """Currencies that actually occur in the data, plus the base currency (these need FX rates)."""
        seen: set[str] = {self.base_currency} if self.base_currency else set()
        for t in self.trades:
            seen.add(t.currency)
        for f in self.fx_trades:
            seen.update((f.bought, f.paid, f.fee_currency))
        for c in self.cash_flows:
            seen.add(c.currency)
        for d in self.dividends:
            seen.add(d.currency)
        return sorted(c for c in seen if c)

    def currencies(self) -> list[str]:
        """Section 7 rows: base currency first, then USD and EUR (always shown), then any other active currency."""
        active = set(self.active_currencies())
        out: list[str] = []
        for c in (self.base_currency, "USD", "EUR"):
            if c and c not in out:
                out.append(c)
        out.extend(sorted(c for c in active if c not in out))
        return out

    def first_activity(self) -> Optional[date]:
        dates: list[date] = [t.dt.date() for t in self.trades] + [f.dt.date() for f in self.fx_trades] \
            + [c.dt.date() for c in self.cash_flows] + [d.dt for d in self.dividends]
        return min(dates) if dates else self.period_start

    def instrument_name(self, ticker: str) -> str:
        inst = self.instruments.get(ticker)
        return inst.name if inst and inst.name else ticker

    # ---- cash (mirror of the spreadsheet's Section 7 formula) --------------------------------------------
    def cash_balances(self) -> dict[str, float]:
        bal: dict[str, float] = defaultdict(float)
        for c in self.cash_flows:
            bal[c.currency] += c.amount
        for d in self.dividends:
            bal[d.currency] += d.total - d.tax_fee
        for t in self.trades:
            bal[t.currency] -= t.quantity * t.price + t.fee
        for f in self.fx_trades:
            bal[f.bought] += f.quantity
            bal[f.paid] -= f.quantity * f.price
            bal[f.fee_currency] -= f.fee
        return {c: bal.get(c, 0.0) for c in self.currencies()}

    def net_quantities(self) -> dict[tuple[str, str], float]:
        q: dict[tuple[str, str], float] = defaultdict(float)
        for t in self.trades:
            q[(t.ticker, t.currency)] += t.quantity
        return dict(q)

    def fingerprint(self) -> str:
        """Hash of every record that ends up in the workbook - detects a stale workbook."""
        payload = _to_jsonable([self.trades, self.fx_trades, self.cash_flows, self.dividends,
                                self.base_currency, self.account])
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def signature(self) -> str:
        """Hash of *everything the workbook displays*: the records plus the reconciliation references (cash report,
        open positions, NAV of the newest statement), the statement list and the warnings.  The in-place refresher
        compares it with the value stored in the workbook; a mismatch means the workbook must be rebuilt."""
        latest = self.latest
        ref = [latest.filename, latest.period_end, latest.cash_report, latest.open_positions, latest.nav] \
            if latest else None
        payload = _to_jsonable([self.fingerprint(), ref, self.alias, self.warnings, self.period_start,
                                self.period_end, self.instruments,
                                [(s.filename, s.fmt, s.period_start, s.period_end) for s in self.statements]])
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------------------
# dividends
# --------------------------------------------------------------------------------------
@dataclass
class _Ev:
    ticker: str
    ccy: str
    dt: date
    key: str
    total: float
    cost: float
    desc: str


def _position_before(trades: Sequence[StockTrade], ticker: str, ccy: str, day: date) -> float:
    return sum(t.quantity for t in trades if t.ticker == ticker and t.currency == ccy and t.dt.date() < day)


def assemble_dividends(lines: Sequence[DividendLine], accruals: Sequence[Accrual], trades: Sequence[StockTrade],
                       instruments: dict[str, Instrument]) -> list[DividendEvent]:
    events: list[_Ev] = []
    exact: dict[tuple, list[_Ev]] = defaultdict(list)
    by_day: dict[tuple, list[_Ev]] = defaultdict(list)

    def register(ev: _Ev) -> None:
        events.append(ev)
        exact[(ev.ticker, ev.ccy, ev.dt, ev.key)].append(ev)
        by_day[(ev.ticker, ev.ccy, ev.dt)].append(ev)

    for l in sorted((l for l in lines if l.kind == "dividend"), key=lambda x: (x.dt, x.ticker, x.description, x.amount)):
        register(_Ev(l.ticker, l.currency, l.dt, l.key, l.amount, 0.0, l.description))

    for l in sorted((l for l in lines if l.kind in ("tax", "fee")), key=lambda x: (x.dt, x.ticker, x.description, x.amount)):
        cands = exact.get((l.ticker, l.currency, l.dt, l.key)) or by_day.get((l.ticker, l.currency, l.dt)) or []
        if cands:
            opposite = [e for e in cands if e.total * l.amount < 0]      # reversal pairs with reversal
            ev = (opposite or cands)[0]
        else:                                                           # tax/fee without a dividend that day
            ev = _Ev(l.ticker, l.currency, l.dt, l.key, 0.0, 0.0, l.description)
            register(ev)
        ev.cost += 0.0 - l.amount

    out: list[DividendEvent] = []
    for ev in sorted(events, key=lambda e: (e.dt, e.ticker, e.desc)):
        qty, src, acc_rate = _quantity(ev, accruals, trades)
        rate = parse_rate(ev.desc)
        if rate and rate[0] == ev.ccy:
            per = rate[1]
        elif acc_rate:
            per = acc_rate
        elif qty:
            per = abs(ev.total) / qty
        else:
            per = 0.0
        inst = instruments.get(ev.ticker)
        out.append(DividendEvent(
            ticker=ev.ticker, name=inst.name if inst and inst.name else ev.ticker, quantity=qty, per_share=per,
            total=ev.total, tax_fee=ev.cost, currency=ev.ccy, dt=ev.dt, quantity_source=src))
    return out


def _quantity(ev: _Ev, accruals: Sequence[Accrual], trades: Sequence[StockTrade]) -> tuple[float, str, float]:
    """Shares that earned the dividend: IBKR accrual table first, otherwise derived from the position."""
    cands = [a for a in accruals if a.ticker == ev.ticker and a.currency == ev.ccy and a.pay_date == ev.dt]
    if cands:
        best = min(cands, key=lambda a: abs(abs(a.gross_amount) - abs(ev.total)))
        return best.quantity, "accrual", best.gross_rate
    pos = _position_before(trades, ev.ticker, ev.ccy, ev.dt)
    rate = parse_rate(ev.desc)
    if rate and rate[0] == ev.ccy and rate[1] > 0 and ev.total:
        if pos > 0 and abs(pos * rate[1] - abs(ev.total)) <= 0.02 * max(1.0, abs(ev.total)) + 0.01:
            return pos, "position", 0.0
        return round(abs(ev.total) / rate[1], 4), "rate", 0.0
    if pos > 0:
        return pos, "position", 0.0
    return 0.0, "none", 0.0


# --------------------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------------------
def merge_statements(statements: Sequence[ParsedStatement], account: Optional[str] = None) -> Portfolio:
    if not statements:
        raise ValueError("No statements to merge.")
    # oldest first; for the same period the CSV (full precision) sorts last so it becomes ``latest`` and is the
    # reference of the reconciliation (the HTML rendering rounds amounts to cents)
    pool = sorted(statements, key=lambda s: (s.period_end or date.min, s.period_start or date.min,
                                             s.fmt == "csv", s.filename))
    if account is None:
        account = pool[-1].account
    chosen = [s for s in pool if s.account == account]
    other = sorted({s.account for s in pool if s.account != account})
    warnings: list[str] = []
    if other:
        warnings.append(f"Statements of other accounts were ignored: {', '.join(other)}.")

    dropped = 0

    def merge(attr: str, key_fn, amount_fn=None) -> list:
        nonlocal dropped
        merged, n = merge_multiset([getattr(s, attr) for s in chosen], key_fn, amount_fn)
        dropped += n
        return merged

    trades = merge("trades", lambda t: (t.ticker, t.currency, t.dt, round(t.quantity, 8), round(t.price, 8)))
    fx = merge("fx_trades", lambda t: (t.bought, t.paid, t.dt, round(t.quantity, 8), round(t.price, 8)))
    flows = merge("cash_flows", lambda c: (c.kind, c.currency, c.dt, c.description), lambda c: c.amount)
    lines = merge("div_lines", lambda l: (l.kind, l.ticker, l.currency, l.dt, l.description), lambda l: l.amount)
    accruals = merge("accruals", lambda a: (a.ticker, a.currency, a.dt, a.pay_date, a.code, round(a.quantity, 6)),
                     lambda a: a.gross_amount)

    instruments: dict[str, Instrument] = {}
    for s in chosen:                                       # oldest -> newest, newer non-empty values win
        for i in s.instruments:
            old = instruments.get(i.ticker)
            if old is None:
                instruments[i.ticker] = i
            else:
                for f in ("name", "conid", "isin", "exchange", "type"):
                    if getattr(i, f):
                        setattr(old, f, getattr(i, f))

    trades.sort(key=lambda t: (t.dt, t.ticker, t.quantity))
    fx.sort(key=lambda t: (t.dt, t.bought, t.quantity))
    flows.sort(key=lambda c: (c.dt, c.kind, c.amount))
    for t in trades:
        inst = instruments.get(t.ticker)
        if inst and inst.name:
            t.name = inst.name
    dividends = assemble_dividends(lines, accruals, trades, instruments)

    starts = [s.period_start for s in chosen if s.period_start]
    ends = [s.period_end for s in chosen if s.period_end]
    latest = chosen[-1]
    base = latest.base_currency or next((s.base_currency for s in chosen if s.base_currency), "")

    # ---- health checks on the set of statements --------------------------------------------------------
    for s in chosen:
        for w in s.warnings:
            warnings.append(f"{s.filename}: {w}")
    spans = sorted((s.period_start, s.period_end, s.filename) for s in chosen if s.period_start and s.period_end)
    reach = None
    for a, b, name in spans:
        if reach is not None and a > reach + timedelta(days=1):
            warnings.append(f"Gap in statement coverage between {reach} and {a} - missing statements make cash "
                            f"balances and positions differ from IBKR.")
        reach = max(reach, b) if reach else b
    first = min(chosen, key=lambda s: s.period_start or date.max)
    start_cash = [l for l in first.cash_report if l.line.lower() == "starting cash" and abs(l.total) > 0.005
                  and l.currency != "Base Currency Summary"]
    if start_cash:
        warnings.append(
            "The earliest statement starts with non-zero cash (" +
            ", ".join(f"{l.currency} {l.total:,.2f}" for l in start_cash) +
            ") - earlier statements are missing, so balances computed from transactions will not match IBKR.")

    return Portfolio(
        account=account, alias=latest.alias, holder=latest.holder, base_currency=base,
        period_start=min(starts) if starts else None, period_end=max(ends) if ends else None,
        trades=trades, fx_trades=fx, cash_flows=flows, dividends=dividends, instruments=instruments,
        statements=chosen, latest=latest, warnings=warnings, duplicates_dropped=dropped)
