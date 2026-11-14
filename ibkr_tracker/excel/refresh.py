"""Create, update *in place* or rebuild the workbook file on disk.

* **updated** - the file is an up-to-date workbook of this app (same layout version, same archive signature):
  only the market-data sheets (Prices / FX / Benchmark / About) are rewritten.  Every formula, format and
  comment of the Portfolio and Checks sheets stays untouched - this is what the *Refresh Prices* button does.
* **rebuilt** - the statement archive changed (new transactions, other account/base currency ...), the file was
  made by another layout version, or the *hide closed positions and dust balances* setting was switched (Sections 6 / 7
  list different rows): the workbook is generated again; the previous file is kept as ``*.previous.xlsx``.
* **created** - no file yet.
"""
from __future__ import annotations

import math
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment

from ..config import hide_closed_dust
from ..market.data import MarketData
from ..portfolio import Portfolio
from . import layout as L
from . import overrides as OV
from .builder import build_workbook, refresh_market_sheets
from .recalc import RecalcResult, embed_values

REQUIRED_SHEETS = (L.PORTFOLIO, L.PRICES, L.FXSHEET, L.BENCH, L.CHECKS, L.ABOUT)


class WorkbookLocked(PermissionError):
    """The .xlsx is open in Excel (or read-only) and cannot be replaced."""


@dataclass
class WorkbookUpdate:
    action: str                                   # created | updated | rebuilt
    path: Path
    reason: str = ""
    backup: Optional[Path] = None
    recalc: Optional[RecalcResult] = None
    messages: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------------------
def _restore_comment_sizes(wb: Workbook) -> None:
    """openpyxl forgets the box size of cell comments when it loads a file - give them a sensible one again."""
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                c: Optional[Comment] = cell.comment
                if c is not None:
                    c.width = 340
                    c.height = int(min(260, max(50, 28 + 15 * math.ceil(len(c.text or "") / 52))))


FILTER_CHANGED = "the 'Hide closed positions and dust balances' setting changed"


def open_current(path: Path, portfolio: Portfolio, hide: bool = False) -> tuple[Optional[Workbook], str]:
    """``(workbook, "")`` if ``path`` is an up-to-date workbook of this app for ``portfolio``, else ``(None, why)``.

    ``hide`` is the view filter (closed positions / dust balances left out of Sections 6 and 7): a file built with the
    other setting has another geometry and is stale.  Without the filter a v1.1.0 workbook stays current."""
    if not path.exists():
        return None, "no workbook yet"
    try:
        wb = load_workbook(path)
    except Exception as exc:                                    # noqa: BLE001
        return None, f"existing file cannot be read ({exc.__class__.__name__})"
    state = OV.read_state(wb)
    if not state:
        return None, "existing file was not created by this app"
    layout = str(state.get("layout") or "")
    if layout.removesuffix(L.FILTER_TAG) != L.WORKBOOK_VERSION:
        return None, "existing file uses another layout version"
    if layout != L.layout_tag(hide):
        return None, FILTER_CHANGED
    missing = [s for s in REQUIRED_SHEETS if s not in wb.sheetnames]
    if missing:
        return None, "existing file lacks the sheet(s) " + ", ".join(missing)
    if state.get("archive") != portfolio.signature():
        return None, "the statement archive changed (new or removed transactions)"
    return wb, ""


def sync_overrides(path: Path | str, settings: dict, portfolio: Portfolio) -> tuple[dict, list[str]]:
    """Three-way merge of the override cells typed into the workbook with the app settings.

    Returns ``(settings_with_merged_overrides, messages)``; the caller persists the settings.  Does nothing when
    the file does not exist or was not written by this app.
    """
    path = Path(path)
    if not path.exists():
        return settings, []
    try:
        wb = load_workbook(path)
        if not OV.read_state(wb):
            return settings, []
        cells = OV.read_cells(wb)
        snapshot = OV.read_snapshot(wb)
    except Exception:                                           # noqa: BLE001 - unreadable file: just ignore it
        return settings, []
    fx_ccys = sorted({*portfolio.currencies(), *(settings.get("fx_overrides") or {}),
                      *(k[3:] for k in cells if k.startswith("fx:"))})
    app = OV.from_settings(settings, portfolio.positions_keys(), fx_ccys)
    merged, notes = OV.merge(cells, snapshot, app)
    return OV.into_settings(merged, settings), notes


def _backup(path: Path) -> Path:
    dest = path.with_name(path.stem + ".previous.xlsx")
    shutil.copy2(path, dest)
    return dest


def _save(wb: Workbook, path: Path) -> None:
    """Atomic save: write next to the target, then replace it (never leaves a half-written workbook)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp.xlsx")
    wb.save(tmp)
    try:
        os.replace(tmp, path)
    except PermissionError as exc:
        tmp.unlink(missing_ok=True)
        raise WorkbookLocked(f"{path.name} is open in another program (Excel?). Close it and try again.") from exc


def refresh_workbook(path: Path | str, portfolio: Portfolio, md: MarketData, settings: dict, *,
                     generated_at: Optional[datetime] = None, recalc: Optional[bool] = None) -> WorkbookUpdate:
    """Bring the workbook at ``path`` up to date with ``portfolio`` and ``md`` (see the module docstring)."""
    path = Path(path)
    existed = path.exists()
    wb, reason = open_current(path, portfolio, hide_closed_dust(settings))
    backup: Optional[Path] = None
    if wb is not None:
        _restore_comment_sizes(wb)
        refresh_market_sheets(wb, portfolio, md, settings, generated_at)
        _save(wb, path)
        action = "updated"
    else:
        if existed:
            try:
                backup = _backup(path)
            except OSError:
                backup = None
        wb = build_workbook(portfolio, md, settings, generated_at)
        _save(wb, path)
        action = "rebuilt" if existed else "created"
    result = WorkbookUpdate(action=action, path=path, reason=reason if action != "updated" else "", backup=backup)
    do_recalc = settings.get("recalc_with_libreoffice", True) if recalc is None else recalc
    if do_recalc:
        result.recalc = embed_values(path)
        if not result.recalc.ok:
            result.messages.append(result.recalc.message)
    return result
