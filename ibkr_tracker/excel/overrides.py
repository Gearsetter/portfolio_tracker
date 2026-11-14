"""The yellow, user-overridable cells of the workbook <-> the app settings.

Two editors can change the same numbers: the person typing into the workbook and the person using the app's
*Market data* tab.  To keep *both* kinds of edit, the app stores a snapshot of the overrides it last wrote into the
workbook (hidden ``_state`` sheet) and performs a three-way merge before every refresh:

    workbook cell != snapshot   -> the cell was edited in Excel     -> keep the workbook value
    otherwise                   -> use the app's value (which may have been edited in the app)

Flat key space (values are ``float`` or ``None`` for "no override")::

    px:<TICKER>|<CCY>   stock price override        fx:<CCY>   units of CCY per USD
    bm                  benchmark price (USD)       days       days per year used for the CAGR
"""
from __future__ import annotations

import json
import math
from typing import Any, Optional

from . import layout as L

Flat = dict[str, Optional[float]]


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        v = v.strip().replace(" ", "").replace(",", ".") if v.strip() else None
        if v is None:
            return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def same(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)


def fx_rows(currencies: list[str], fx_cols: list[str]) -> list[str]:
    """Rows of the 'latest FX' table on the Prices sheet (USD first, then the rest)."""
    return ["USD"] + [c for c in currencies if c != "USD"] + [c for c in fx_cols if c not in currencies and c != "USD"]


# ---- settings / market data  ->  flat ------------------------------------------------------------------------
def from_settings(settings: dict, positions: list[tuple[str, str]], fx_ccys: list[str]) -> Flat:
    po = settings.get("price_overrides") or {}
    fo = settings.get("fx_overrides") or {}
    out: Flat = {}
    for ticker, ccy in positions:
        key = f"{ticker}|{ccy}"
        out["px:" + key] = _num(po.get(key))
    for c in fx_ccys:
        if c != "USD":
            out["fx:" + c] = _num(fo.get(c))
    out["bm"] = _num(settings.get("benchmark_override"))
    out["days"] = _num(settings.get("days_per_year", 365))
    return out


def _set(target: dict, key: str, value: Optional[float]) -> None:
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value


def into_settings(flat: Flat, settings: dict) -> dict:
    """Copy of ``settings`` with the merged overrides applied (``None`` removes an override)."""
    new = dict(settings)
    po = dict(settings.get("price_overrides") or {})
    fo = dict(settings.get("fx_overrides") or {})
    for k, v in flat.items():
        if k.startswith("px:"):
            _set(po, k[3:], v)
        elif k.startswith("fx:"):
            _set(fo, k[3:], v)
        elif k == "bm":
            new["benchmark_override"] = v
        elif k == "days" and v:
            new["days_per_year"] = v
    new["price_overrides"], new["fx_overrides"] = po, fo
    return new


# ---- workbook cells  ->  flat -----------------------------------------------------------------------------------
def read_cells(wb) -> Flat:
    """Override cells of an existing workbook (formulas untouched; works for openpyxl workbooks in either mode)."""
    ws = wb[L.PRICES]
    out: Flat = {}
    c = L.PX_STOCK_COLS
    r = L.PX_STOCK_HDR + 1
    while ws[c["ticker"] + str(r)].value not in (None, ""):
        key = "{}|{}".format(ws[c["ticker"] + str(r)].value, ws[c["ccy"] + str(r)].value)
        out["px:" + key] = _num(ws[c["override"] + str(r)].value)
        r += 1
    fc = L.PX_FX_COLS
    r = L.PX_STOCK_HDR + 1
    while True:
        ccy = ws[fc["ccy"] + str(r)].value
        if not (isinstance(ccy, str) and len(ccy) == 3 and ccy.isupper()):
            break
        if ccy != "USD":
            out["fx:" + ccy] = _num(ws[fc["override"] + str(r)].value)
        r += 1
    out["bm"] = _num(ws[L.PX_BENCH_COLS["override"] + str(L.PX_BENCH_ROW)].value)
    out["days"] = _num(ws["B" + str(L.PX_SETTINGS["days"])].value)
    return out


# ---- snapshot (hidden sheet) -----------------------------------------------------------------------------------
STATE_ROWS = dict(layout=1, archive=2, account=3, overrides=4)


def read_state(wb) -> dict[str, str]:
    if L.STATE not in wb.sheetnames:
        return {}
    ws = wb[L.STATE]
    return {k: ws.cell(r, 2).value for k, r in STATE_ROWS.items() if ws.cell(r, 2).value is not None}


def read_snapshot(wb) -> Flat:
    raw = read_state(wb).get("overrides")
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return {k: _num(v) for k, v in data.items()} if isinstance(data, dict) else {}


def describe(key: str) -> str:
    """Human-readable name of a flat key for messages."""
    if key.startswith("px:"):
        ticker, _, ccy = key[3:].partition("|")
        return f"price override {ticker} ({ccy})"
    if key.startswith("fx:"):
        return f"FX override USD.{key[3:]}"
    return {"bm": "benchmark price override", "days": "days per year"}.get(key, key)


def merge(workbook: Flat, snapshot: Flat, app: Flat) -> tuple[Flat, list[str]]:
    """Three-way merge; returns ``(merged, messages)`` where messages list overrides adopted from the workbook."""
    merged: Flat = {}
    notes: list[str] = []
    for k in sorted({*workbook, *snapshot, *app}):
        if k in workbook and not same(workbook[k], snapshot.get(k)):
            merged[k] = workbook[k]                      # edited in Excel since the app last wrote the file
            if not same(workbook[k], app.get(k)):
                value = "cleared" if workbook[k] is None else f"{workbook[k]:g}"
                notes.append(f"Took the {describe(k)} from the workbook: {value}.")
        else:
            merged[k] = app.get(k)
    return merged, notes
