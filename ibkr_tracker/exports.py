"""CSV export of a portfolio snapshot (one account or the consolidated all-accounts view).

The Excel workbook is the primary export (``excel/``); this module offers the same tables as plain CSV files - one
file per table, UTF-8 with a byte order mark so that Excel opens them correctly.  In the consolidated view every
record table starts with an ``Account`` column, so the merged transaction history stays traceable.

Two dialects: ``international`` (comma separated, decimal point - what pandas, R, Google Sheets ... expect) and
``european`` (semicolon separated, decimal comma - what a double click in Excel needs under a Hungarian, German,
French ... regional setting, where the comma is the decimal mark and the semicolon the list separator).
"""
from __future__ import annotations

import io
import zipfile
from typing import Optional

import pandas as pd

from . import tables as T
from .config import hide_closed_dust
from .tracker import Snapshot

CSV_STYLES: dict[str, dict[str, str]] = {
    "international": dict(sep=",", decimal="."),
    "european": dict(sep=";", decimal=","),
}
CSV_STYLE_LABELS = {
    "international": "International (comma separated, decimal point)",
    "european": "European Excel (semicolon separated, decimal comma)",
}
DEFAULT_CSV_STYLE = "international"


def csv_style(settings: Optional[dict] = None, style: Optional[str] = None) -> str:
    """Explicit ``style`` > the ``csv_format`` setting > international."""
    for candidate in (style, (settings or {}).get("csv_format")):
        if candidate in CSV_STYLES:
            return candidate
    return DEFAULT_CSV_STYLE


def _decimal_comma(df: pd.DataFrame) -> pd.DataFrame:
    """pandas leaves the floats inside *object* columns alone (the summary table mixes text, dates and numbers):
    give those a decimal comma by hand so that the whole file follows the same convention."""
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = [repr(v).replace(".", ",") if isinstance(v, float) and v == v else v for v in out[col]]
    return out


def csv_tables(snap: Snapshot) -> dict[str, pd.DataFrame]:
    """``{file name: table}`` - empty tables are left out.  With the view filter on (closed positions with zero
    shares, cash balances below 0.5) ``positions.csv`` and ``cash.csv`` do not list them; the transaction tables are
    history and always complete, and so are the totals in ``summary.csv``."""
    m, p = snap.metrics, snap.portfolio
    if m is None:
        return {}
    acct = p.is_consolidated
    hide = hide_closed_dust(snap.settings)
    tables = {
        "summary.csv": T.summary_table(m, p, hide),
        "positions.csv": T.positions_table(m, hide),
        "cash.csv": T.cash_table(m, hide),
        "stock_transactions.csv": T.trades_table(m, acct),
        "dividends.csv": T.dividends_table(p, acct),
        "deposits_withdrawals.csv": T.deposits_table(m, acct),
        "forex.csv": T.forex_table(p, acct),
    }
    if acct:
        tables["accounts.csv"] = T.accounts_table(snap.accounts)
    if snap.checks:
        tables["checks.csv"] = T.checks_table(snap.checks)
    return {name: df for name, df in tables.items() if len(df)}


def csv_files(snap: Snapshot, style: Optional[str] = None) -> dict[str, bytes]:
    """``{file name: bytes}`` in the requested dialect (default: the ``csv_format`` setting of the snapshot)."""
    style = csv_style(snap.settings, style)
    kw = CSV_STYLES[style]
    out = {}
    for name, df in csv_tables(snap).items():
        out[name] = (_decimal_comma(df) if style == "european" else df).to_csv(index=False, **kw).encode("utf-8-sig")
    return out


def csv_zip(snap: Snapshot, style: Optional[str] = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in csv_files(snap, style).items():
            z.writestr(name, data)
    return buf.getvalue()
