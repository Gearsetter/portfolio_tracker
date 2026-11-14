"""Price providers. Only Yahoo Finance is implemented; anything with a ``history`` method can be plugged in.

The Yahoo access is deliberately defensive: ``yfinance`` is tried first (it copes with Yahoo's cookie/crumb
handling), the plain chart endpoint is the fallback, and every failure is reported instead of raised so the
rest of the application can fall back to cached / statement-derived data.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, Protocol
from urllib.parse import quote

# quote currencies Yahoo reports in minor units
MINOR_UNITS = {"GBp": ("GBP", 0.01), "GBX": ("GBP", 0.01), "ZAc": ("ZAR", 0.01), "ZAC": ("ZAR", 0.01),
               "ILA": ("ILS", 0.01), "KWF": ("KWD", 0.001)}


class ProviderError(RuntimeError):
    pass


@dataclass
class SeriesResult:
    symbol: str
    rows: list[tuple[date, float]]          # (day, close) ascending, already scaled to major currency units
    currency: Optional[str] = None
    name: Optional[str] = None
    notes: list[str] = field(default_factory=list)


class PriceProvider(Protocol):
    def history(self, symbol: str, start: date, end: date, include_intraday: bool = False) -> SeriesResult: ...


# --------------------------------------------------------------------------------------
# pure helpers (unit-tested without network access)
# --------------------------------------------------------------------------------------
def normalise_currency(code: Optional[str]) -> tuple[Optional[str], float]:
    """``"GBp"`` -> (``"GBP"``, 0.01) so pence quotes become pounds."""
    if not code:
        return None, 1.0
    if code in MINOR_UNITS:
        return MINOR_UNITS[code]
    return code.upper(), 1.0


def drop_incomplete_bar(rows: list[tuple[date, float]], session_end_epoch: Optional[float], gmtoffset: float,
                        now_utc: datetime) -> list[tuple[date, float]]:
    """Remove today's bar while the exchange is still open - the spreadsheet wants *closing* prices."""
    if not rows or session_end_epoch is None:
        return rows
    local_today = (now_utc + timedelta(seconds=gmtoffset or 0)).date()
    if rows[-1][0] == local_today and now_utc.timestamp() < float(session_end_epoch):
        return rows[:-1]
    return rows


def parse_chart_json(symbol: str, payload: dict[str, Any], now_utc: Optional[datetime] = None,
                     include_intraday: bool = False) -> SeriesResult:
    """Parse the JSON returned by ``/v8/finance/chart`` into a :class:`SeriesResult`."""
    chart = (payload or {}).get("chart") or {}
    if chart.get("error"):
        err = chart["error"]
        raise ProviderError(f"{symbol}: {err.get('code', 'error')} - {err.get('description', '')}".strip(" -"))
    results = chart.get("result") or []
    if not results:
        raise ProviderError(f"{symbol}: empty response")
    res = results[0]
    meta = res.get("meta") or {}
    stamps = res.get("timestamp") or []
    quote_block = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote_block.get("close") or []
    offset = float(meta.get("gmtoffset") or 0)
    rows: list[tuple[date, float]] = []
    for ts, close in zip(stamps, closes):
        if close is None:
            continue
        day = (datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(seconds=offset)).date()
        rows.append((day, float(close)))
    ccy, scale = normalise_currency(meta.get("currency"))
    if scale != 1.0:
        rows = [(d, v * scale) for d, v in rows]
    if not include_intraday:
        regular = ((meta.get("currentTradingPeriod") or {}).get("regular") or {})
        rows = drop_incomplete_bar(rows, regular.get("end"), offset, now_utc or datetime.now(timezone.utc))
    # de-duplicate days (keep the last value) and sort
    by_day = {d: v for d, v in rows}
    clean = sorted(by_day.items())
    if not clean:
        raise ProviderError(f"{symbol}: no price rows returned")
    return SeriesResult(symbol=symbol, rows=clean, currency=ccy,
                        name=meta.get("longName") or meta.get("shortName"))


# --------------------------------------------------------------------------------------
# Yahoo Finance
# --------------------------------------------------------------------------------------
class YahooProvider:
    """Daily closes from Yahoo Finance (unadjusted closes - what a broker shows)."""

    CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/124.0 Safari/537.36", "Accept": "application/json"}

    def __init__(self, timeout: float = 20.0, retries: int = 2, pause: float = 0.25, use_yfinance: bool = True):
        self.timeout = timeout
        self.retries = retries
        self.pause = pause
        self.use_yfinance = use_yfinance

    # public ---------------------------------------------------------------------------------------------
    def history(self, symbol: str, start: date, end: date, include_intraday: bool = False) -> SeriesResult:
        errors: list[str] = []
        if self.use_yfinance:
            try:
                return self._via_yfinance(symbol, start, end, include_intraday)
            except Exception as exc:                       # noqa: BLE001 - report every failure mode
                errors.append(f"yfinance: {exc}")
        try:
            return self._via_chart_api(symbol, start, end, include_intraday)
        except Exception as exc:                           # noqa: BLE001
            errors.append(f"chart API: {exc}")
        raise ProviderError("; ".join(errors))

    # implementations -------------------------------------------------------------------------------------
    def _via_yfinance(self, symbol: str, start: date, end: date, include_intraday: bool) -> SeriesResult:
        import logging

        import yfinance as yf                              # imported lazily: optional dependency

        logging.getLogger("yfinance").setLevel(logging.CRITICAL)      # failures are reported through ProviderError
        ticker = yf.Ticker(symbol)
        df = ticker.history(start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(), interval="1d",
                            auto_adjust=False, actions=False)
        if df is None or len(df) == 0 or "Close" not in df:
            raise ProviderError(f"{symbol}: no data")
        rows = [(ts.date(), float(v)) for ts, v in df["Close"].dropna().items()]
        meta: dict[str, Any] = {}
        try:
            meta = dict(ticker.history_metadata or {})
        except Exception:                                  # noqa: BLE001 - metadata is optional
            pass
        ccy, scale = normalise_currency(meta.get("currency"))
        if scale != 1.0:
            rows = [(d, v * scale) for d, v in rows]
        if not include_intraday:
            regular = ((meta.get("currentTradingPeriod") or {}).get("regular") or {})
            end_epoch = regular.get("end")
            if isinstance(end_epoch, datetime):
                end_epoch = end_epoch.timestamp()
            if isinstance(end_epoch, (int, float)):
                rows = drop_incomplete_bar(rows, float(end_epoch), float(meta.get("gmtoffset") or 0),
                                           datetime.now(timezone.utc))
        if not rows:
            raise ProviderError(f"{symbol}: no price rows")
        return SeriesResult(symbol=symbol, rows=sorted(dict(rows).items()), currency=ccy,
                            name=meta.get("longName") or meta.get("shortName"))

    def _via_chart_api(self, symbol: str, start: date, end: date, include_intraday: bool) -> SeriesResult:
        import requests

        params = {
            "period1": int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()),
            "period2": int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 2 * 86400,
            "interval": "1d", "includeAdjustedClose": "true", "events": "div,splits",
        }
        last: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                resp = requests.get(self.CHART_URL.format(symbol=quote(symbol, safe="")), params=params,
                                    headers=self.HEADERS, timeout=self.timeout)
                if resp.status_code == 429:
                    raise ProviderError("rate limited (HTTP 429)")
                if resp.status_code >= 400 and resp.status_code != 404:
                    raise ProviderError(f"HTTP {resp.status_code}")
                return parse_chart_json(symbol, resp.json(), include_intraday=include_intraday)
            except Exception as exc:                       # noqa: BLE001
                last = exc
                time.sleep(self.pause * (attempt + 1))
        raise ProviderError(str(last))
