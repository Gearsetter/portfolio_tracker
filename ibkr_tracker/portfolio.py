"""Merge any number of parsed statements into one chronologically ordered :class:`Portfolio`.

Merge rules
-----------
* Statements of different accounts are never mixed: :func:`merge_statements` builds one account at a time and
  :func:`consolidate` then combines several account portfolios into one *consolidated* portfolio.
* The same transaction that appears in several statements (overlapping periods, or the CSV *and* the HTML
  rendering of one statement) is kept **once**.  Matching is multiplicity aware: two identical fills inside
  one statement are two transactions, but "1 in file A + 1 in file B" is still one.
* When duplicates differ only by precision the full-precision record (CSV) wins over the rounded (HTML) one.
* Dividends, withholding taxes and dividend fees are synchronised by (ticker, date, dividend description).

Share counts
------------
Stock splits and other corporate actions change the number of shares without a trade, so neither the "Corporate
Actions" section nor the sum of the transactions is used to decide what is held.  The **Open Positions** section of the
**chronologically latest statement of the account** is the source of truth (``Portfolio.holdings``).  Only when no
statement has such a section (a custom statement without it) the counts are derived from the transactions and a
warning says so.  The sum of the transactions is still computed - it feeds the info checks and the factor that
expresses transaction quantities in today's shares.
"""
from __future__ import annotations

import dataclasses
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
QTY_TOL = 1e-6               # share counts closer than this are the same
DUST_THRESHOLD = 0.5         # a cash balance below this (absolute, in its own currency) is "dust" for the view filter
CONSOLIDATED = "ALL"         # account id of the consolidated (all accounts merged) portfolio
CONSOLIDATED_LABEL = "All accounts (consolidated)"

# where the share counts of a portfolio come from (``Portfolio.holdings_source``)
SRC_OPEN_POSITIONS = "open_positions"                   # Open Positions section of the latest statement
SRC_OPEN_PLUS_TRADES = "open_positions+transactions"    # newest Open Positions + the trades after that statement
SRC_TRANSACTIONS = "transactions"                       # fallback: no statement has an Open Positions section


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
def is_closed(quantity: float) -> bool:
    """A position with (practically) no shares left - fully closed."""
    return abs(quantity) < QTY_TOL


def is_dust(balance: float) -> bool:
    """A residual cash balance: absolute amount below :data:`DUST_THRESHOLD` (in the balance's own currency)."""
    return abs(balance) < DUST_THRESHOLD


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
    # share counts: (ticker, currency) -> shares, taken from the Open Positions of the latest statement (see the
    # module docstring).  ``holdings_source`` is empty for a hand-made Portfolio without that information.
    holdings: dict[tuple[str, str], float] = field(default_factory=dict)
    holdings_source: str = ""
    holdings_ref: str = ""                  # human readable: which statement the share counts come from
    notes: list[str] = field(default_factory=list)          # informational (not problems), e.g. split detected
    members: list["Portfolio"] = field(default_factory=list)   # consolidated portfolio only: the account portfolios

    # ---- identity ---------------------------------------------------------------------------------------
    @property
    def is_consolidated(self) -> bool:
        return self.account == CONSOLIDATED

    @property
    def account_ids(self) -> list[str]:
        """The real account ids behind this portfolio (one for an ordinary portfolio, several if consolidated)."""
        return [m.account for m in self.members] if self.members else [self.account]

    @property
    def label(self) -> str:
        return CONSOLIDATED_LABEL if self.is_consolidated else self.account

    def latest_statements(self) -> list[ParsedStatement]:
        """The newest statement of every account - the reference of the reconciliation."""
        if self.members:
            return [s for m in self.members for s in m.latest_statements()]
        return [self.latest] if self.latest else []

    # ---- derived lists ---------------------------------------------------------------------------------
    def positions_keys(self) -> list[tuple[str, str]]:
        """Unique (ticker, currency) of every stock ever bought *or held according to the statement*, alphabetical
        (Section 6 rows).  A holding without a trade in the archive (transfer, spin-off, earlier history that is
        not imported) still gets its row because the Open Positions section lists it."""
        keys = {(t.ticker, t.currency) for t in self.trades}
        if self.holdings_source:
            keys.update(self.holdings)
        return sorted(keys)

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
        seen.update(m.base_currency for m in self.members)        # consolidated: each account's own base currency
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

    # ---- view filter: closed positions and dust balances ------------------------------------------------
    # Display only: the totals, returns and CAGR always include every position and every balance.
    def visible_positions(self, hide: bool = False) -> list[tuple[str, str]]:
        """:meth:`positions_keys` without the fully closed positions (zero shares) when ``hide`` is on."""
        keys = self.positions_keys()
        if not hide:
            return keys
        held = self.net_quantities()
        return [k for k in keys if not is_closed(held.get(k, 0.0))]

    def visible_currencies(self, hide: bool = False) -> list[str]:
        """:meth:`currencies` without the dust balances (|balance| < 0.5) when ``hide`` is on."""
        ccys = self.currencies()
        if not hide:
            return ccys
        bal = self.cash_balances()
        return [c for c in ccys if not is_dust(bal.get(c, 0.0))]

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

    def transaction_quantities(self) -> dict[tuple[str, str], float]:
        """Sum of the signed transaction quantities per (ticker, currency).  NOT the share count: splits and other
        corporate actions are invisible here - see :meth:`net_quantities`."""
        q: dict[tuple[str, str], float] = defaultdict(float)
        for t in self.trades:
            q[(t.ticker, t.currency)] += t.quantity
        return dict(q)

    def net_quantities(self) -> dict[tuple[str, str], float]:
        """Shares held per (ticker, currency): the Open Positions of the latest statement (source of truth)."""
        if self.holdings_source:
            return dict(self.holdings)
        return self.transaction_quantities()

    def quantity_differences(self) -> list[tuple[str, str, float, float]]:
        """(ticker, currency, statement shares, transaction sum) wherever the two disagree - a stock split, a
        transfer, a spin-off or statements that do not reach back to the first purchase."""
        held, tx = self.net_quantities(), self.transaction_quantities()
        out = []
        for key in self.positions_keys():
            h, t = held.get(key, 0.0), tx.get(key, 0.0)
            if abs(h - t) > QTY_TOL:
                out.append((key[0], key[1], h, t))
        return out

    def share_factors(self) -> dict[tuple[str, str], float]:
        """Statement shares / transaction sum per position (1.0 when they agree or no transaction exists).

        Transaction quantities are stated in the shares of the day they were executed; multiplying them by this
        factor expresses them in today's shares (a 10:1 split gives 10), so that "current value" columns of the
        individual transactions add up to the value of the position."""
        held, tx = self.net_quantities(), self.transaction_quantities()
        out = {}
        for key in self.positions_keys():
            t = tx.get(key, 0.0)
            out[key] = held.get(key, 0.0) / t if abs(t) > QTY_TOL else 1.0
        return out

    def reference_cash(self) -> dict[str, float]:
        """'Ending Cash' per currency printed in the newest statement(s) - summed over the accounts."""
        out: dict[str, float] = defaultdict(float)
        for s in self.latest_statements():
            for l in s.cash_report:
                if l.line == "Ending Cash" and l.currency != "Base Currency Summary":
                    out[l.currency] += l.total
        return dict(out)

    def reference_nav(self) -> Optional[float]:
        """Net asset value (base currency) printed in the newest statement(s); ``None`` when not comparable."""
        total = 0.0
        for s in self.latest_statements():
            if "total" not in s.nav or (s.base_currency and s.base_currency != self.base_currency):
                return None
            total += s.nav["total"]
        return total if self.latest_statements() else None

    def fingerprint(self) -> str:
        """Hash of every record that ends up in the workbook - detects a stale workbook."""
        payload = _to_jsonable([self.trades, self.fx_trades, self.cash_flows, self.dividends,
                                self.base_currency, self.account])
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def signature(self) -> str:
        """Hash of *everything the workbook displays*: the records plus the reconciliation references (cash report,
        open positions, NAV of the newest statement of every account), the share counts, the statement list and the
        warnings.  The in-place refresher compares it with the value stored in the workbook; a mismatch means the
        workbook must be rebuilt."""
        refs = [[s.filename, s.account, s.period_end, s.cash_report, s.open_positions, s.open_positions_section, s.nav]
                for s in self.latest_statements()]
        payload = _to_jsonable([self.fingerprint(), refs, self.alias, self.warnings, self.notes, self.period_start,
                                self.period_end, self.instruments, self.holdings_source, self.holdings_ref,
                                sorted(self.holdings.items()),
                                [(s.filename, s.fmt, s.account, s.period_start, s.period_end) for s in self.statements]])
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
# share counts: the Open Positions section of the latest statement is the source of truth
# --------------------------------------------------------------------------------------
def _fmt_qty(q: float) -> str:
    text = f"{q:,.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _stmt_ref(s: ParsedStatement) -> str:
    return f"{s.filename} (period end {s.period_end})"


def open_position_counts(s: ParsedStatement) -> dict[tuple[str, str], float]:
    """Shares per (ticker, currency) according to the Open Positions section of one statement."""
    out: dict[tuple[str, str], float] = defaultdict(float)
    for op in s.open_positions:
        out[(op.ticker, op.currency)] += op.quantity
    return {k: v for k, v in out.items() if abs(v) > 1e-9}


def _statement_says_flat(s: ParsedStatement) -> bool:
    """No Open Positions section, but the Net Asset Value table proves that no stock is held (IBKR leaves the
    section out of an account without positions)."""
    if "stock" in s.nav:
        return abs(s.nav["stock"]) < 0.5
    return "total" in s.nav and "cash" in s.nav


def determine_holdings(chosen: Sequence[ParsedStatement], trades: Sequence[StockTrade]
                       ) -> tuple[dict[tuple[str, str], float], str, str, list[str]]:
    """Share counts of one account -> ``(holdings, source, reference text, warnings)``.

    ``chosen`` is sorted oldest -> newest, so ``chosen[-1]`` is the chronologically latest statement.  Its Open
    Positions section decides; corporate actions (splits, mergers, spin-offs) and transaction sums are *not*
    consulted.  Fallbacks, in order, each with a warning:

    1. the latest statement has no such section but proves a flat account (NAV without stocks) -> nothing held;
    2. an older statement has one -> its Open Positions plus the trades dated after that statement;
    3. no statement has one -> the transaction sums.
    """
    latest = chosen[-1]
    if latest.has_open_positions:
        return open_position_counts(latest), SRC_OPEN_POSITIONS, _stmt_ref(latest), []
    if _statement_says_flat(latest):
        return {}, SRC_OPEN_POSITIONS, f"{_stmt_ref(latest)} (Net Asset Value shows no stock)", []
    anchor = next((s for s in reversed(chosen) if s.has_open_positions and s.period_end), None)
    if anchor is not None:
        held: dict[tuple[str, str], float] = defaultdict(float, open_position_counts(anchor))
        for t in trades:
            if t.dt.date() > anchor.period_end:
                held[(t.ticker, t.currency)] += t.quantity
        counts = {k: v for k, v in held.items() if abs(v) > 1e-9}
        return counts, SRC_OPEN_PLUS_TRADES, _stmt_ref(anchor), [
            f"{latest.filename} has no Open Positions section: the share counts are those of {_stmt_ref(anchor)} plus "
            f"the transactions after that date - a stock split after it would not be reflected. Include 'Open "
            f"Positions' in the Activity Statement."]
    tx: dict[tuple[str, str], float] = defaultdict(float)
    for t in trades:
        tx[(t.ticker, t.currency)] += t.quantity
    return dict(tx), SRC_TRANSACTIONS, "sum of the transactions", [
        "No statement contains an Open Positions section, so the share counts are the sum of the transactions - "
        "stock splits and transfers are NOT reflected. Include 'Open Positions' in the Activity Statement."]


def quantity_notes(p: Portfolio, prefix: str = "") -> list[str]:
    """Informational note when the statement's share count differs from the transaction sum (split, transfer...)."""
    if p.holdings_source in ("", SRC_TRANSACTIONS):
        return []
    diffs = p.quantity_differences()
    if not diffs:
        return []
    shown = "; ".join(f"{t} ({c}): {_fmt_qty(h)} in the statement, {_fmt_qty(x)} from the transactions"
                      for t, c, h, x in diffs[:6])
    more = f"; {len(diffs) - 6} more" if len(diffs) > 6 else ""
    return [f"{prefix}Share counts come from the Open Positions section of {p.holdings_ref}. They differ from the sum "
            f"of the transactions for {shown}{more} - a stock split, transfer or history before the first imported "
            f"statement. The statement count is used."]


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
    for rec in (*trades, *fx, *flows, *dividends):          # remember where every record comes from
        rec.account = account

    starts = [s.period_start for s in chosen if s.period_start]
    ends = [s.period_end for s in chosen if s.period_end]
    latest = chosen[-1]
    base = latest.base_currency or next((s.base_currency for s in chosen if s.base_currency), "")
    holdings, source, ref, holding_warnings = determine_holdings(chosen, trades)
    warnings.extend(holding_warnings)

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

    p = Portfolio(
        account=account, alias=latest.alias, holder=latest.holder, base_currency=base,
        period_start=min(starts) if starts else None, period_end=max(ends) if ends else None,
        trades=trades, fx_trades=fx, cash_flows=flows, dividends=dividends, instruments=instruments,
        statements=chosen, latest=latest, warnings=warnings, duplicates_dropped=dropped,
        holdings=holdings, holdings_source=source, holdings_ref=ref)
    p.notes = [f"{s.filename}: {n}" for s in chosen for n in s.notes] + quantity_notes(p)
    return p


# --------------------------------------------------------------------------------------
# consolidated view: all accounts as one portfolio
# --------------------------------------------------------------------------------------
def consolidate(portfolios: Sequence[Portfolio], base_currency: Optional[str] = None) -> Portfolio:
    """Merge the portfolios of several accounts into one unified :class:`Portfolio` (``account == CONSOLIDATED``).

    * transactions, dividends and deposits of all accounts are concatenated chronologically; every record keeps its
      ``account``; nothing is de-duplicated across accounts (two accounts can both buy 10 shares on one day);
    * the share counts are the **sum of the accounts' Open Positions** (each account from its own latest statement);
    * cash balances, USD values, returns and CAGR follow from the combined records - each cash flow is converted
      with its own currency and date, so accounts with different base currencies combine without distortion;
    * ``base_currency`` is the currency of the "Summary (base)" section: the given one, else the common base
      currency of the accounts, else USD.
    """
    if not portfolios:
        raise ValueError("No portfolios to consolidate.")
    members = sorted(portfolios, key=lambda q: q.account)
    bases = sorted({m.base_currency for m in members if m.base_currency})
    base = (base_currency or "").strip().upper() or (bases[0] if len(bases) == 1 else "USD")

    trades = sorted((t for m in members for t in m.trades), key=lambda t: (t.dt, t.account, t.ticker, t.quantity))
    fx = sorted((f for m in members for f in m.fx_trades), key=lambda f: (f.dt, f.account, f.bought, f.quantity))
    flows = sorted((c for m in members for c in m.cash_flows), key=lambda c: (c.dt, c.account, c.kind, c.amount))
    dividends = sorted((d for m in members for d in m.dividends), key=lambda d: (d.dt, d.ticker, d.account))

    instruments: dict[str, Instrument] = {}
    for m in members:
        for ticker, inst in m.instruments.items():
            old = instruments.get(ticker)
            if old is None:
                instruments[ticker] = dataclasses.replace(inst)
            else:
                for f in ("name", "conid", "isin", "exchange", "type"):
                    if getattr(inst, f) and not getattr(old, f):
                        setattr(old, f, getattr(inst, f))

    holdings: dict[tuple[str, str], float] = defaultdict(float)
    for m in members:
        for key, qty in m.net_quantities().items():
            holdings[key] += qty
    sources = {m.holdings_source for m in members}
    if sources == {SRC_TRANSACTIONS}:
        source = SRC_TRANSACTIONS
    elif sources == {SRC_OPEN_POSITIONS}:
        source = SRC_OPEN_POSITIONS
    else:
        source = SRC_OPEN_PLUS_TRADES

    statements = sorted((s for m in members for s in m.statements),
                        key=lambda s: (s.period_end or date.min, s.period_start or date.min, s.fmt == "csv",
                                       s.account, s.filename))
    newest = [m.latest for m in members if m.latest]
    latest = max(newest, key=lambda s: (s.period_end or date.min, s.account)) if newest else None
    starts = [m.period_start for m in members if m.period_start]
    ends = [m.period_end for m in members if m.period_end]

    warnings = [f"[{m.account}] {w}" for m in members for w in m.warnings]
    notes = [f"[{m.account}] {n}" for m in members for n in m.notes]
    if len(bases) > 1:
        notes.append(f"The accounts have different base currencies ({', '.join(bases)}). Every amount is converted via "
                     f"USD; the consolidated base-currency summary is shown in {base}.")
    last_days = {m.account: m.period_end for m in members if m.period_end}
    if len(set(last_days.values())) > 1:
        warnings.append("The accounts' newest statements end on different days (" +
                        ", ".join(f"{a}: {d}" for a, d in sorted(last_days.items())) +
                        ") - each account's share counts and cash are as of its own newest statement.")

    holders = list(dict.fromkeys(m.holder for m in members if m.holder))
    aliases = list(dict.fromkeys(m.alias or m.account for m in members))
    return Portfolio(
        account=CONSOLIDATED, alias=" + ".join(aliases), holder=" / ".join(holders), base_currency=base,
        period_start=min(starts) if starts else None, period_end=max(ends) if ends else None,
        trades=trades, fx_trades=fx, cash_flows=flows, dividends=dividends, instruments=instruments,
        statements=statements, latest=latest, warnings=warnings,
        duplicates_dropped=sum(m.duplicates_dropped for m in members),
        holdings=dict(holdings), holdings_source=source,
        holdings_ref="; ".join(f"{m.account}: {m.holdings_ref}" for m in members), notes=notes, members=list(members))
