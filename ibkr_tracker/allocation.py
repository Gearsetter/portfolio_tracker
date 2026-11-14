"""Allocation of the portfolio: by asset class, stock type, industry sector, currency or holding.

Where the classification comes from (highest priority first)

* **asset class** - the user's own entry (``settings["classification"]``), else derived from the *Type* of the
  *Financial Instrument Information* section of the statement: an ETF / fund is ``ETFs & funds``, everything else
  (common stock, ADR, REIT, preferred ...) is ``Stocks``; cash balances are ``Cash``;
* **stock type** - the statement's *Type* (COMMON, ADR, ETF, REIT, PREFERRED ...), shown with a readable name;
* **industry sector** - the user's own entry, else the sector Yahoo Finance reports for the stock (looked up on request
  and cached in the archive, see :meth:`ibkr_tracker.market.service.MarketDataService.refresh_profiles`); an ETF
  without a sector is ``ETFs & funds``, a stock nobody classified is ``Unclassified``.  IBKR statements carry no sector
  information at all.

The weights are shares of the **positive** USD values (stocks and cash): a negative cash balance (a margin loan) cannot
be a slice of a donut - it is listed separately.  All values are the ones of the dashboard (the same prices and rates,
the view filter that hides closed positions and dust balances is honoured).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .calc import Metrics
from .portfolio import Portfolio, is_fund_type

CASH = "Cash"
STOCKS = "Stocks"
FUNDS = "ETFs & funds"
UNCLASSIFIED = "Unclassified"
UNKNOWN_TYPE = "Unknown type"

GROUPINGS: dict[str, str] = {
    "asset_class": "Asset class", "stock_type": "Stock type", "sector": "Industry sector",
    "currency": "Currency", "holding": "Holding"}

# Yahoo Finance's sector names (what an automatic lookup returns) - a manual entry is matched to them
SECTORS = ("Technology", "Healthcare", "Financial Services", "Consumer Cyclical", "Consumer Defensive", "Industrials",
           "Energy", "Utilities", "Real Estate", "Basic Materials", "Communication Services")
SECTOR_ALIASES = {
    "information technology": "Technology", "tech": "Technology", "it": "Technology",
    "health care": "Healthcare", "health": "Healthcare", "financials": "Financial Services",
    "financial": "Financial Services", "finance": "Financial Services", "consumer discretionary": "Consumer Cyclical",
    "consumer staples": "Consumer Defensive", "materials": "Basic Materials", "communication": "Communication Services",
    "communications": "Communication Services", "telecommunication services": "Communication Services",
    "telecommunications": "Communication Services", "telecom": "Communication Services",
    "industrial": "Industrials", "utility": "Utilities", "realestate": "Real Estate",
}
ASSET_CLASSES = (STOCKS, FUNDS, "Bonds", "Commodities", "Real estate", "Crypto", "Other")

TYPE_LABELS = {
    "COMMON": "Common stock", "ADR": "ADR (depositary receipt)", "ETF": "ETF", "REIT": "REIT",
    "PREFERRED": "Preferred stock", "UNIT": "Unit", "RIGHT": "Right", "WARRANT": "Warrant",
    "CLOSED-END FUND": "Closed-end fund", "MUTUAL FUND": "Mutual fund", "ETN": "ETN",
}

MAX_NAME = 40
EPS = 1e-9


# ---------------------------------------------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------------------------------------------
def type_label(ibkr_type: str) -> str:
    """Readable name of the statement's *Type* column (``COMMON`` -> ``Common stock``)."""
    t = (ibkr_type or "").strip()
    if not t:
        return UNKNOWN_TYPE
    return TYPE_LABELS.get(t.upper(), t.title())


def is_fund(ibkr_type: str, quote_type: str = "") -> bool:
    """ETFs, ETNs and funds - judged by the statement's type, or by Yahoo's ``quoteType`` when the statement has none."""
    if (ibkr_type or "").strip():
        return is_fund_type(ibkr_type)
    return (quote_type or "").upper() in ("ETF", "MUTUALFUND", "ETN")


def canonical_sector(text: str) -> str:
    """A sector as typed by the user, matched (case-insensitively, with the usual GICS names) to Yahoo's sector names;
    anything else is kept as typed (whitespace collapsed)."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    low = t.lower()
    for name in SECTORS:
        if low == name.lower():
            return name
    return SECTOR_ALIASES.get(low, t)


def canonical_asset_class(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    low = t.lower()
    for name in (*ASSET_CLASSES, CASH):
        if low == name.lower():
            return name
    aliases = {"stock": STOCKS, "equity": STOCKS, "equities": STOCKS, "shares": STOCKS, "etf": FUNDS, "etfs": FUNDS,
               "fund": FUNDS, "funds": FUNDS, "bond": "Bonds", "fixed income": "Bonds", "gold": "Commodities",
               "commodity": "Commodities", "reit": "Real estate"}
    return aliases.get(low, t)


def clean_name(text: str, canonical) -> tuple[str, str]:
    """``(name, error)`` for a user entry: whitespace collapsed, matched to the known names, at most ``MAX_NAME``
    characters, no control characters.  An empty entry is valid (it removes the override)."""
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if not t:
        return "", ""
    if len(t) > MAX_NAME:
        return "", f"'{t[:12]}...' is longer than {MAX_NAME} characters"
    if any(ord(c) < 32 for c in t):
        return "", "contains control characters"
    return canonical(t), ""


# ---------------------------------------------------------------------------------------------------------------
# the holdings with their classification
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class Holding:
    key: str                          # "TICKER|CCY" (cash: "Cash CCY")
    ticker: str                       # cash: "Cash USD"
    currency: str
    name: str
    usd_value: float
    ibkr_type: str = ""
    asset_class: str = STOCKS
    stock_type: str = ""
    sector: str = UNCLASSIFIED
    industry: str = ""
    auto_asset_class: str = STOCKS    # before the user's entry
    auto_sector: str = UNCLASSIFIED
    class_manual: bool = False        # asset class typed by the user
    sector_manual: bool = False
    is_cash: bool = False
    quantity: float = 0.0

    @property
    def class_source(self) -> str:
        return "your entry" if self.class_manual else "statement type"

    @property
    def sector_source(self) -> str:
        """Where the sector of a stock comes from (shown in the classification table)."""
        if self.is_cash:
            return "cash"
        if self.sector_manual:
            return "your entry"
        if self.auto_sector == FUNDS:
            return "fund (no sector)"
        if self.auto_sector == UNCLASSIFIED:
            return "not classified yet"
        return "Yahoo Finance"

    def group(self, by: str) -> str:
        if by == "asset_class":
            return self.asset_class
        if by == "stock_type":
            return self.stock_type
        if by == "sector":
            return self.sector
        if by == "currency":
            return self.currency
        if by == "holding":
            return self.ticker
        raise ValueError(f"unknown grouping {by!r}")


def _override(overrides: dict, key: str, ticker: str) -> dict:
    """The user's entry of ``key`` (``"TICKER|CCY"``); an entry of the bare ticker applies to every listing of it."""
    for k in (key, ticker):
        v = overrides.get(k)
        if isinstance(v, dict):
            return v
    return {}


def classify(portfolio: Portfolio, metrics: Metrics, settings: dict, profiles: Optional[dict] = None,
             hide: bool = False) -> list[Holding]:
    """The stocks held now and the cash balances, each with its asset class, type and sector (see the module docstring).
    ``profiles`` is ``MarketData.profiles`` (the cached Yahoo sectors); ``hide`` the dashboard's view filter."""
    profiles = profiles or {}
    overrides = settings.get("classification") or {}
    out: list[Holding] = []
    for r in metrics.visible_positions(hide):
        if abs(r.usd_value) <= EPS:
            continue
        key = f"{r.ticker}|{r.currency}"
        inst = portfolio.instruments.get(r.ticker)
        itype = inst.type if inst else ""
        prof = profiles.get(key) or {}
        mine = _override(overrides, key, r.ticker)
        fund = is_fund(itype, prof.get("quote_type", ""))
        auto_class = FUNDS if fund else STOCKS
        sector_auto = (prof.get("sector") or "").strip() or (FUNDS if fund else UNCLASSIFIED)
        cls = (mine.get("asset_class") or "").strip()
        sec = (mine.get("sector") or "").strip()
        out.append(Holding(
            key=key, ticker=r.ticker, currency=r.currency, name=r.name, usd_value=r.usd_value, ibkr_type=itype,
            asset_class=cls or auto_class, stock_type=type_label(itype), sector=sec or sector_auto,
            industry=(prof.get("industry") or "").strip(), auto_asset_class=auto_class, auto_sector=sector_auto,
            class_manual=bool(cls), sector_manual=bool(sec), quantity=r.quantity))
    for c in metrics.visible_cash(hide):
        if abs(c.usd_value) < 0.005:
            continue
        label = f"Cash {c.currency}"
        out.append(Holding(key=label, ticker=label, currency=c.currency, name=f"Cash ({c.currency})",
                           usd_value=c.usd_value, asset_class=CASH, stock_type=CASH, sector=CASH,
                           auto_asset_class=CASH, auto_sector=CASH, is_cash=True, quantity=c.balance))
    return out


# ---------------------------------------------------------------------------------------------------------------
# allocation by one grouping
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class Slice:
    label: str
    usd_value: float
    weight: float
    members: list[str] = field(default_factory=list)       # tickers (cash lines as "Cash USD")


@dataclass
class Allocation:
    by: str
    slices: list[Slice] = field(default_factory=list)      # positive values, largest first
    total: float = 0.0                                     # their sum - the 100 %
    excluded: list[tuple[str, float]] = field(default_factory=list)   # negative balances / short positions

    @property
    def unclassified_share(self) -> float:
        return sum(s.weight for s in self.slices if s.label in (UNCLASSIFIED, UNKNOWN_TYPE))

    def get(self, label: str) -> Optional[Slice]:
        return next((s for s in self.slices if s.label == label), None)


def allocate(holdings: list[Holding], by: str) -> Allocation:
    """Weights of the ``holdings`` grouped by ``by`` (a key of :data:`GROUPINGS`)."""
    if by not in GROUPINGS:
        raise ValueError(f"unknown grouping {by!r}")
    sums: dict[str, float] = {}
    parts: dict[str, list[tuple[float, str]]] = {}
    alloc = Allocation(by)
    for h in holdings:
        if h.usd_value <= EPS:
            if h.usd_value < -0.005:
                alloc.excluded.append((h.ticker, h.usd_value))
            continue
        g = h.group(by)
        sums[g] = sums.get(g, 0.0) + h.usd_value
        parts.setdefault(g, []).append((h.usd_value, h.ticker))
    alloc.total = sum(sums.values())
    ordered = sorted(sums.items(), key=lambda kv: (-kv[1], kv[0]))
    # the members of a slice are listed largest first (ties by name)
    members = {g: [t for _, t in sorted(p, key=lambda x: (-x[0], x[1]))] for g, p in parts.items()}
    alloc.slices = [Slice(g, v, v / alloc.total if alloc.total > EPS else 0.0, members[g]) for g, v in ordered]
    return alloc


def chart_slices(alloc: Allocation, max_slices: int = 10) -> list[Slice]:
    """The slices of the donut: when there are more than ``max_slices`` the smallest ones are merged into ``Other (n)``
    (the table below the chart keeps every one)."""
    s = alloc.slices
    if len(s) <= max_slices:
        return list(s)
    head, tail = s[: max_slices - 1], s[max_slices - 1:]
    other = Slice(f"Other ({len(tail)})", sum(x.usd_value for x in tail), sum(x.weight for x in tail),
                  [m for x in tail for m in x.members])
    return [*head, other]


# ---------------------------------------------------------------------------------------------------------------
# concentration
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class Concentration:
    n_positions: int = 0
    largest_ticker: str = ""
    largest_weight: Optional[float] = None       # share of the whole portfolio (stocks + positive cash)
    top3_weight: Optional[float] = None
    top5_weight: Optional[float] = None
    effective_positions: Optional[float] = None  # 1 / sum(w^2) of the stock weights - the number of equal positions
    cash_weight: float = 0.0


def concentration(holdings: list[Holding]) -> Concentration:
    """How concentrated the portfolio is: the biggest positions as a share of the whole portfolio and the effective number
    of positions (the inverse Herfindahl index of the stock weights - 10 equal positions = 10.0)."""
    stocks = sorted((h for h in holdings if not h.is_cash and h.usd_value > EPS), key=lambda h: -h.usd_value)
    cash = sum(h.usd_value for h in holdings if h.is_cash and h.usd_value > EPS)
    out = Concentration(n_positions=len(stocks))
    total = sum(h.usd_value for h in stocks) + cash
    if not stocks or total <= EPS:
        out.cash_weight = 1.0 if cash > EPS else 0.0
        return out
    out.largest_ticker = stocks[0].ticker
    out.largest_weight = stocks[0].usd_value / total
    out.top3_weight = sum(h.usd_value for h in stocks[:3]) / total
    out.top5_weight = sum(h.usd_value for h in stocks[:5]) / total
    s_total = sum(h.usd_value for h in stocks)
    out.effective_positions = 1.0 / sum((h.usd_value / s_total) ** 2 for h in stocks)
    out.cash_weight = cash / total
    return out


# ---------------------------------------------------------------------------------------------------------------
# the user's entries (Dashboard > Portfolio analysis > Classify your holdings)
# ---------------------------------------------------------------------------------------------------------------
@dataclass
class ClassificationChange:
    classification: dict = field(default_factory=dict)       # the new settings value
    changed: list[str] = field(default_factory=list)         # keys whose entry changed
    errors: list[str] = field(default_factory=list)


def apply_classification_edits(settings: dict, holdings: list[Holding],
                               edits: dict[str, tuple[str, str]]) -> ClassificationChange:
    """Merge the edits of the shown rows - ``{"TICKER|CCY": (asset class, sector)}`` as typed by the user - into the saved
    entries.  An entry equal to the automatic value (or empty) is not an override and is removed."""
    result = ClassificationChange(classification={k: dict(v) for k, v in (settings.get("classification") or {}).items()
                                                  if isinstance(v, dict)})
    by_key = {h.key: h for h in holdings if not h.is_cash}
    for key, (cls_text, sec_text) in edits.items():
        h = by_key.get(key)
        if h is None:
            continue
        cls, e1 = clean_name(cls_text, canonical_asset_class)
        sec, e2 = clean_name(sec_text, canonical_sector)
        if e1 or e2:
            result.errors.append(f"{h.ticker}: {e1 or e2}")
            continue
        entry = {}
        if cls and cls != h.auto_asset_class:
            entry["asset_class"] = cls
        if sec and sec != h.auto_sector:
            entry["sector"] = sec
        old = result.classification.get(key, {})
        if entry != old:
            result.changed.append(key)
            if entry:
                result.classification[key] = entry
            else:
                result.classification.pop(key, None)
    return result
