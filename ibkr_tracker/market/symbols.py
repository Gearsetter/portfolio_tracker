"""Map IBKR tickers to Yahoo Finance symbols."""
from __future__ import annotations

import re
from typing import Mapping, Optional

# IBKR "Listing Exch" -> Yahoo suffix (US listings need none).
EXCHANGE_SUFFIX: dict[str, str] = {
    # United States
    "NASDAQ": "", "NYSE": "", "ARCA": "", "AMEX": "", "NYSE ARCA": "", "BATS": "", "ISLAND": "", "IEX": "",
    "PINK": "", "OTC": "", "NYSEMKT": "", "NASDAQ.NMS": "", "SMART": "",
    # Germany / Europe
    "IBIS": ".DE", "IBIS2": ".DE", "XETRA": ".DE", "FWB": ".F", "FWB2": ".F", "SWB": ".SG", "GETTEX": ".MU",
    "TGATE": ".DE", "AEB": ".AS", "SBF": ".PA", "BM": ".MC", "BVME": ".MI", "BVME.ETF": ".MI", "EBS": ".SW",
    "VSE": ".VI", "BVL": ".LS", "HEX": ".HE", "SFB": ".ST", "CPH": ".CO", "OMXNO": ".OL", "OSE": ".OL",
    "WSE": ".WA", "PRA": ".PR", "ISE": ".IR", "ENEXT.BE": ".BR", "IBEX": ".MC",
    # Hungary
    "BUX": ".BD", "BSE": ".BD",
    # United Kingdom
    "LSE": ".L", "LSEETF": ".L", "LSEIOB1": ".L",
    # Canada / Asia-Pacific
    "TSE": ".TO", "VENTURE": ".V", "SEHK": ".HK", "TSEJ": ".T", "ASX": ".AX", "SGX": ".SI", "NSE": ".NS",
}


def yahoo_symbol(ticker: str, exchange: str = "", currency: str = "",
                 overrides: Optional[Mapping[str, str]] = None) -> str:
    """Best-effort Yahoo symbol; ``overrides["TICKER|CCY"]`` (or ``overrides["TICKER"]``) always wins."""
    overrides = overrides or {}
    for key in (f"{ticker}|{currency}", ticker):
        if overrides.get(key):
            return str(overrides[key]).strip()
    exch = (exchange or "").upper().strip()
    suffix = EXCHANGE_SUFFIX.get(exch, "")
    base = ticker.strip()
    if suffix == ".HK" and base.isdigit():
        base = base.zfill(4)
    if suffix == "" or exch in ("", "SMART"):
        base = re.sub(r"[\s]+", "-", base)             # IBKR "BRK B" -> Yahoo "BRK-B"
    return base + suffix
