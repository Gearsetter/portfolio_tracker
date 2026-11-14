"""Paths and default settings."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SETTINGS: dict[str, Any] = {
    "benchmark_symbol": "VUAA.L",      # Vanguard S&P 500 UCITS ETF (USD, Acc) on the London Stock Exchange - quoted in USD
    "price_source": "yahoo",           # "yahoo" (online, falls back to statements) | "statement" (offline only)
    "export_excel": True,              # the "export results as Excel" toggle
    "output_dir": "",                  # empty -> <data dir>/output
    "days_per_year": 365,              # used by the time-weighted CAGR
    "recalc_with_libreoffice": True,   # store cached values in the .xlsx when LibreOffice is installed
    "yahoo_symbols": {},               # "TICKER|CCY" -> Yahoo Finance symbol (overrides the automatic mapping)
    "manual_only": [],                 # "TICKER|CCY" keys that are never downloaded: manual price history only
    "price_overrides": {},             # "TICKER|CCY" -> manual price
    "fx_overrides": {},                # "CCY" -> manual units of CCY per 1 USD
    "benchmark_override": None,        # manual benchmark price in USD
    "last_account": "",
    "consolidated_base": "",           # base currency of the all-accounts view; empty = common base currency, else USD
    "csv_format": "international",     # CSV dialect: "international" (, and .) | "european" (; and decimal comma)
    "hide_closed_dust": False,         # view filter: hide positions with 0 shares and cash balances below 0.5
    "classification": {},              # "TICKER|CCY" -> {"asset_class": ..., "sector": ...}: the user's own entries
    "risk_free": {},                   # "CCY" -> annual risk-free rate in percent (Sharpe / Sortino / alpha); empty = 0
}


def data_dir() -> Path:
    """Folder holding the archive database, the output workbooks and the settings.

    Override with the ``IBKR_TRACKER_HOME`` environment variable; defaults to ``./data`` next to ``app.py``.
    """
    env = os.environ.get("IBKR_TRACKER_HOME")
    base = Path(env).expanduser() if env else PROJECT_ROOT / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def output_dir(settings: dict[str, Any] | None = None) -> Path:
    custom = (settings or {}).get("output_dir") or ""
    out = Path(custom).expanduser() if custom else data_dir() / "output"
    out.mkdir(parents=True, exist_ok=True)
    return out


def hide_closed_dust(settings: dict | None) -> bool:
    """The view filter (closed positions with zero shares + cash balances below 0.5): on or off."""
    return bool((settings or {}).get("hide_closed_dust"))
