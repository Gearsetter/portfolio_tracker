"""Test doubles. NOTHING here is shipped data: it only exists so tests can exercise the refresh pipeline
without network access (Yahoo Finance is unreachable from CI sandboxes)."""
from __future__ import annotations

import math
import zlib
from datetime import date, timedelta

from ibkr_tracker.market.providers import ProfileResult, ProviderError, SeriesResult

BASE_LEVELS = {            # symbol -> (level, quote currency)
    "HUF=X": (330.0, "HUF"), "EUR=X": (0.86, "EUR"), "GBP=X": (0.75, "GBP"),
    "VUAA.L": (105.0, "USD"), "VUAA.DE": (92.0, "EUR"),
}


# symbol -> (sector, industry, quoteType): what the fake "Yahoo" answers to a profile lookup (test data only)
BASE_PROFILES = {
    "AMZN": ("Consumer Cyclical", "Internet Retail", "EQUITY"), "MSFT": ("Technology", "Software - Infrastructure", "EQUITY"),
    "GOOGL": ("Communication Services", "Internet Content & Information", "EQUITY"),
    "META": ("Communication Services", "Internet Content & Information", "EQUITY"),
    "BN": ("Financial Services", "Asset Management", "EQUITY"), "NVO": ("Healthcare", "Drug Manufacturers - General", "EQUITY"),
    "AAPL": ("Technology", "Consumer Electronics", "EQUITY"), "SAP.DE": ("Technology", "Software - Application", "EQUITY"),
    "TSLA": ("Consumer Cyclical", "Auto Manufacturers", "EQUITY"), "KO": ("Consumer Defensive", "Beverages - Non-Alcoholic", "EQUITY"),
    "VUAA.L": ("", "", "ETF"),
}


class FakeProvider:
    """Deterministic pseudo-random walk on weekdays; records every call for assertions."""

    def __init__(self, fail: tuple[str, ...] = (), levels: dict[str, float] | None = None, last_day: date | None = None,
                 currencies: dict[str, str] | None = None, profiles: dict[str, tuple[str, str, str]] | None = None,
                 profile_fail: tuple[str, ...] = ()):
        self.calls: list[tuple[str, date, date]] = []
        self.fail = set(fail)
        self.levels = levels or {}
        self.last_day = last_day
        self.currencies = currencies or {}             # symbol -> quote currency (default: USD, like most test symbols)
        self.profiles = {**BASE_PROFILES, **(profiles or {})}      # symbol -> (sector, industry, quoteType)
        self.profile_fail = set(profile_fail)
        self.profile_calls: list[str] = []

    def history(self, symbol: str, start: date, end: date, include_intraday: bool = False) -> SeriesResult:
        self.calls.append((symbol, start, end))
        if symbol in self.fail:
            raise ProviderError(f"{symbol}: simulated outage")
        level, ccy = BASE_LEVELS.get(symbol, (self.levels.get(symbol, 100.0), "USD"))
        level = self.levels.get(symbol, level)
        ccy = self.currencies.get(symbol, ccy)
        seed = zlib.crc32(symbol.encode()) % 997
        rows = []
        d = start
        stop = min(end, self.last_day) if self.last_day else end
        while d <= stop:
            if d.weekday() < 5:
                n = (d - date(2026, 1, 1)).days
                rows.append((d, round(level * (1 + 0.0004 * n + 0.01 * math.sin((n + seed) / 9.0)), 6)))
            d += timedelta(days=1)
        if not rows:
            raise ProviderError(f"{symbol}: no rows")
        return SeriesResult(symbol=symbol, rows=rows, currency=ccy, name=f"Fake {symbol}")

    def profile(self, symbol: str) -> ProfileResult:
        self.profile_calls.append(symbol)
        if symbol in self.profile_fail:
            raise ProviderError(f"{symbol}: simulated outage")
        if symbol not in self.profiles:
            raise ProviderError(f"{symbol}: not found")
        sector, industry, qtype = self.profiles[symbol]
        return ProfileResult(symbol, sector, industry, qtype, f"Fake {symbol}")
