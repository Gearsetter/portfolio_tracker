"""Ticker mapping: which external (Yahoo Finance) symbol belongs to which IBKR ticker.

IBKR and the data providers do not always agree on a name (``BRK B`` vs ``BRK-B``, ``NOVOB`` vs ``NOVO-B.CO``, a
London listing that IBKR reports without suffix ...).  The automatic rule (:func:`~ibkr_tracker.market.symbols.yahoo_symbol`:
listing exchange -> suffix) gets most of them right; for the others the user types the correct symbol - an **alias** -
and it is used from then on for every download, every workbook and every account.

Where the data live
-------------------
* ``settings["yahoo_symbols"]``: ``{"TICKER|CCY": "SYMBOL"}`` - the aliases (a plain ``"TICKER"`` key applies to every
  currency).  Stored in the SQLite settings of the data folder, so they survive restarts, upgrades and a new browser.
* ``settings["manual_only"]``: ``["TICKER|CCY", ...]`` - stocks that are never downloaded because the provider does
  not know them at all; their prices come from the manual price history (:mod:`ibkr_tracker.market.manual`).

Aliases can be exported to / imported from a small CSV file (``Ticker,Currency,Symbol,Manual only``) - a backup, a way
to share a mapping between machines and the "mapping file" for people who prefer to edit it in a spreadsheet.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional

from ..portfolio import Portfolio, is_closed
from ..storage import Store
from .providers import PriceProvider, ProviderError
from .service import MarketDataService

# Yahoo symbols: letters, digits and . - = ^ _ (BRK-B, NOVO-B.CO, ^GSPC, EURUSD=X, 0700.HK, 7203.T)
_SYMBOL = re.compile(r"^[A-Za-z0-9^][A-Za-z0-9.\-=^_&]{0,31}$")
HEADERS = ("Ticker", "Currency", "Symbol", "Manual only")


def validate_symbol(symbol: str) -> Optional[str]:
    """``None`` when ``symbol`` can be a Yahoo Finance symbol, otherwise what is wrong with it."""
    s = (symbol or "").strip()
    if not s:
        return "the symbol is empty"
    if re.search(r"\s", s):
        return "a symbol must not contain blanks (Yahoo writes 'BRK-B' for IBKR's 'BRK B')"
    if not _SYMBOL.match(s):
        return "only letters, digits and . - = ^ _ are allowed in a symbol"
    return None


def clean_symbol(symbol: Optional[str]) -> str:
    """Normalise what the user typed (blanks around it, lower case): Yahoo symbols are upper case."""
    return (symbol or "").strip().upper()


@dataclass
class MappingRow:
    ticker: str
    currency: str
    name: str
    exchange: str
    held: bool                      # shares in the newest statement
    auto_symbol: str                # what the automatic rule gives
    alias: str                      # the user's override ("" = none)
    manual_only: bool
    status: str = ""
    last_price: Optional[date] = None
    points: int = 0                 # downloaded prices in the cache
    manual_points: int = 0
    problem: bool = False

    @property
    def key(self) -> str:
        return f"{self.ticker}|{self.currency}"

    @property
    def symbol(self) -> str:
        """The symbol that is actually used for the download."""
        return self.alias or self.auto_symbol

    @property
    def source(self) -> str:
        if self.manual_only:
            return "manual prices only"
        return "your alias" if self.alias else "automatic"


def _alias_of(aliases: dict, ticker: str, currency: str) -> str:
    for key in (f"{ticker}|{currency}", ticker):
        if aliases.get(key):
            return str(aliases[key]).strip()
    return ""


def mapping_rows(store: Store, service: MarketDataService, portfolio: Portfolio, settings: dict) -> list[MappingRow]:
    """One row per stock of ``portfolio`` with the automatic symbol, the alias and how the download is going."""
    aliases = settings.get("yahoo_symbols") or {}
    manual_only = service.manual_only(settings)
    held = portfolio.net_quantities()
    manual = store.manual_price_summary()
    rows: list[MappingRow] = []
    for ticker, ccy in portfolio.positions_keys():
        inst = portfolio.instruments.get(ticker)
        key = f"{ticker}|{ccy}"
        row = MappingRow(
            ticker=ticker, currency=ccy, name=portfolio.instrument_name(ticker),
            exchange=inst.exchange if inst else "", held=not is_closed(held.get((ticker, ccy), 0.0)),
            auto_symbol=service.auto_symbol(portfolio, ticker, ccy), alias=_alias_of(aliases, ticker, ccy),
            manual_only=key in manual_only, manual_points=(manual.get(key) or {}).get("count", 0))
        _status(store, row)
        rows.append(row)
    return rows


def _status(store: Store, row: MappingRow) -> None:
    """Fill ``status`` / ``problem`` / ``last_price`` of a row from the cache and the last download error."""
    if row.manual_only:
        if row.manual_points:
            row.status = f"manual prices only - {row.manual_points} price(s) entered"
        else:
            row.status = "manual prices only - none entered yet (Manual price history)"
            row.problem = row.held
        return
    sym = row.symbol
    prices = store.get_prices(sym)
    meta = store.price_meta(sym)
    row.points = len(prices)
    row.last_price = prices[-1][0] if prices else None
    extra = f" + {row.manual_points} manual" if row.manual_points else ""
    quoted = (meta.get("currency") or "").upper()
    if prices and quoted and quoted != row.currency.upper():
        row.status = f"quoted in {quoted}, IBKR trades it in {row.currency} - wrong symbol? cached prices are ignored"
        row.problem = True
    elif meta.get("last_error"):
        err = str(meta["last_error"])
        err = err if len(err) <= 90 else err[:87] + "..."
        row.status = (f"last download failed: {err}" if not prices else
                      f"last download failed ({err}); cached prices up to {row.last_price} are used") + extra
        row.problem = row.held and not (prices or row.manual_points)
    elif prices:
        row.status = f"ok - {len(prices)} prices, newest {row.last_price}{extra}"
    elif row.manual_points:
        row.status = f"no download yet; {row.manual_points} manual price(s)"
    else:
        row.status = "not downloaded yet - press Refresh Prices"


# ---------------------------------------------------------------------------------------------------------------
# applying edits
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class MappingChange:
    aliases: dict[str, str]          # the complete new alias dictionary
    manual_only: list[str]           # the complete new manual-only list
    changed: list[str]               # keys whose effective symbol / mode changed
    errors: list[str]                # edits that were rejected (the rest was applied)


def apply_edits(settings: dict, rows: Iterable[MappingRow], edits: dict[str, tuple[str, bool]]) -> MappingChange:
    """Merge the user's table edits into the saved mapping.

    ``edits``: ``{"TICKER|CCY": (alias typed, manual_only ticked)}`` for the *shown* rows.  Only rows whose entry
    differs from what is saved are touched, and stocks that are not shown (other accounts) stay as they are - saving
    the mapping while looking at one account never erases another's.  An alias equal to the automatic symbol is no
    override and is dropped; an invalid alias is rejected with a message (the other edits are still applied).  Clearing
    the alias of a row also clears a ticker-wide alias (``"TICKER"`` key of an imported file) it came from."""
    aliases = {str(k): str(v) for k, v in (settings.get("yahoo_symbols") or {}).items() if v}
    manual = {str(k) for k in (settings.get("manual_only") or [])}
    by_key = {r.key: r for r in rows}
    changed: list[str] = []
    errors: list[str] = []
    for key, (alias, only) in edits.items():
        row = by_key.get(key)
        if row is None:
            continue
        alias = clean_symbol(alias)
        if alias == row.auto_symbol.upper():
            alias = ""
        if (alias, bool(only)) == (row.alias, row.manual_only):
            continue
        if alias:
            problem = validate_symbol(alias)
            if problem:
                errors.append(f"{row.ticker}: '{alias}' rejected - {problem}.")
                continue
            aliases[key] = alias
        else:
            aliases.pop(key, None)
            aliases.pop(row.ticker, None)
        manual.discard(key)
        if only:
            manual.add(key)
        changed.append(key)
    return MappingChange(aliases, sorted(manual), changed, errors)


def mapping_to_csv(settings: dict) -> str:
    """The saved mapping as ``Ticker,Currency,Symbol,Manual only`` CSV (every account's entries, not just the shown)."""
    aliases = settings.get("yahoo_symbols") or {}
    manual = {str(k) for k in (settings.get("manual_only") or [])}
    keys = sorted({*(str(k) for k in aliases), *manual})
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(HEADERS)
    for key in keys:
        ticker, _, ccy = key.partition("|")
        w.writerow([ticker, ccy, aliases.get(key, ""), "yes" if key in manual else ""])
    return out.getvalue()


def parse_mapping_csv(text: str) -> tuple[dict[str, tuple[str, bool]], list[str]]:
    """Read a mapping file: ``({"TICKER|CCY": (symbol, manual_only)}, problems)``.  Accepts comma, semicolon or tab as
    the separator; the ``Currency`` column may be empty (the alias then applies to every currency of the ticker)."""
    lines = [l for l in text.replace("\r\n", "\n").replace("﻿", "").split("\n") if l.strip()]
    if not lines:
        return {}, ["The file is empty."]
    sep = max((";", "\t", ","), key=lambda c: lines[0].count(c))
    rows = list(csv.reader(lines, delimiter=sep))
    head = [h.strip().lower() for h in rows[0]]
    if "ticker" not in head:
        return {}, ["The first line must name the columns: Ticker, Currency, Symbol, Manual only."]
    col = {name: head.index(name) for name in ("ticker", "currency", "symbol", "manual only") if name in head}
    out: dict[str, tuple[str, bool]] = {}
    problems: list[str] = []
    for n, row in enumerate(rows[1:], 2):
        def cell(name: str) -> str:
            i = col.get(name)
            return row[i].strip() if i is not None and i < len(row) else ""
        ticker, ccy = cell("ticker"), cell("currency").upper()
        sym, only = clean_symbol(cell("symbol")), cell("manual only").lower() in ("yes", "y", "true", "1", "x")
        if not ticker:
            problems.append(f"line {n}: no ticker")
            continue
        if sym:
            bad = validate_symbol(sym)
            if bad:
                problems.append(f"line {n}: '{sym}' rejected - {bad}")
                continue
        if not sym and not only:
            continue                                     # nothing to apply
        out[f"{ticker}|{ccy}" if ccy else ticker] = (sym, only)
    return out, problems


def merge_import(settings: dict, entries: dict[str, tuple[str, bool]]) -> MappingChange:
    """Apply imported entries on top of the saved mapping (entries replace the same key, others stay)."""
    aliases = {str(k): str(v) for k, v in (settings.get("yahoo_symbols") or {}).items() if v}
    manual = {str(k) for k in (settings.get("manual_only") or [])}
    changed: list[str] = []
    for key, (sym, only) in entries.items():
        if (aliases.get(key, ""), key in manual) != (sym, only):
            changed.append(key)
        aliases.pop(key, None)
        manual.discard(key)
        if sym:
            aliases[key] = sym
        if only:
            manual.add(key)
    return MappingChange(aliases, sorted(manual), changed, [])


# ---------------------------------------------------------------------------------------------------------------
# testing a symbol
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class SymbolTest:
    symbol: str
    ok: bool
    message: str
    last_price: Optional[float] = None
    last_day: Optional[date] = None
    currency: str = ""
    name: str = ""
    currency_ok: bool = True


def check_symbol(provider: PriceProvider, symbol: str, expected_currency: str = "",
                today: Optional[date] = None) -> SymbolTest:
    """Ask the provider for the last ten days of ``symbol`` - the quick check behind the *Test* button.  The result
    says whether the symbol exists, what it costs and in which currency it is quoted (a mismatch with the currency
    IBKR trades the stock in usually means a different listing)."""
    sym = clean_symbol(symbol)
    problem = validate_symbol(sym)
    if problem:
        return SymbolTest(sym, False, f"Not a valid symbol: {problem}.")
    end = today or date.today()
    try:
        res = provider.history(sym, end - timedelta(days=14), end)
    except ProviderError as exc:
        return SymbolTest(sym, False, f"Yahoo Finance has no data for {sym}: {exc}")
    except Exception as exc:                                       # noqa: BLE001 - network problems of any kind
        return SymbolTest(sym, False, f"Could not reach Yahoo Finance: {exc.__class__.__name__}: {exc}")
    if not res.rows:
        return SymbolTest(sym, False, f"{sym}: no prices returned.")
    day, price = res.rows[-1]
    quoted = (res.currency or "").upper()
    ok_ccy = not (expected_currency and quoted and quoted != expected_currency.upper())
    msg = f"{sym}: last close {price:,.4f} {quoted} on {day}" + (f" - {res.name}" if res.name else "")
    if not ok_ccy:
        msg += f". Warning: IBKR trades this stock in {expected_currency.upper()}, this symbol is quoted in {quoted}."
    return SymbolTest(sym, True, msg, price, day, quoted, res.name or "", ok_ccy)
