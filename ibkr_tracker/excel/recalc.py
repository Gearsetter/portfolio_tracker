"""Optional step: let LibreOffice calculate the workbook and copy the results into it as *cached values*.

Excel always recalculates the file on open (``fullCalcOnLoad``), so this is only cosmetic - but it makes the file
show numbers in viewers that do not calculate (Quick Look, e-mail previews, pandas, Excel's Protected View ...).

The workbook written by openpyxl is **not** replaced by LibreOffice's re-saved copy (that would alter styles and
internal links); only the ``<v>`` values of the formula cells are injected into the original XML.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import warnings
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from lxml import etree
from openpyxl import load_workbook
from openpyxl.utils.datetime import to_excel

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_ERRORS = {"#NULL!", "#DIV/0!", "#VALUE!", "#REF!", "#NAME?", "#NUM!", "#N/A"}

_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice", "/usr/local/bin/soffice", "/opt/libreoffice/program/soffice",
    "/snap/bin/libreoffice",
)


@dataclass
class RecalcResult:
    ok: bool
    message: str
    formulas: int = 0
    errors: int = 0


def find_soffice() -> Optional[str]:
    """Path of the LibreOffice executable (``SOFFICE_PATH`` env var, PATH, then the usual install folders)."""
    env = os.environ.get("SOFFICE_PATH")
    if env and Path(env).exists():
        return env
    for name in ("soffice", "libreoffice", "soffice.exe"):
        found = shutil.which(name)
        if found:
            return found
    for cand in _CANDIDATES:
        if Path(cand).exists():
            return cand
    return None


def _cell_values(path: Path) -> dict[str, dict[str, Any]]:
    with warnings.catch_warnings():                 # openpyxl cannot parse LibreOffice's chart XML; only values matter here
        warnings.simplefilter("ignore", UserWarning)
        wb = load_workbook(path, data_only=True)
    out: dict[str, dict[str, Any]] = {}
    for ws in wb.worksheets:
        sheet: dict[str, Any] = {}
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None:
                    sheet[c.coordinate] = c.value
        out[ws.title] = sheet
    return out


def _sheet_parts(zin: zipfile.ZipFile) -> dict[str, str]:
    wbx = etree.fromstring(zin.read("xl/workbook.xml"))
    rels = etree.fromstring(zin.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target") for r in rels}
    parts: dict[str, str] = {}
    for s in wbx.find(f"{{{NS_MAIN}}}sheets"):
        target = targets[s.get(f"{{{NS_REL}}}id")]
        parts[s.get("name")] = target[1:] if target.startswith("/") else "xl/" + target
    return parts


def _set_cached(c: etree._Element, val: Any) -> bool:
    """Write ``val`` as the cached result of formula cell ``c``; returns True when it is an Excel error."""
    v = c.find(f"{{{NS_MAIN}}}v")
    if v is None:
        v = etree.SubElement(c, f"{{{NS_MAIN}}}v")
    is_error = False
    if isinstance(val, bool):
        c.set("t", "b")
        v.text = "1" if val else "0"
    elif isinstance(val, (int, float)):
        c.attrib.pop("t", None)
        v.text = repr(float(val)) if isinstance(val, float) else str(val)
    elif isinstance(val, (datetime, date)):
        c.attrib.pop("t", None)
        v.text = repr(float(to_excel(val)))
    elif isinstance(val, str) and val in _ERRORS:
        c.set("t", "e")
        v.text = val
        is_error = True
    else:                                           # text, or None -> empty string result
        c.set("t", "str")
        v.text = "" if val is None else str(val)
    return is_error


def inject_values(path: Path, values: dict[str, dict[str, Any]]) -> tuple[int, int]:
    """Copy ``values`` (sheet -> coordinate -> value) into the formula cells of ``path``; returns (n, n_errors)."""
    path = Path(path)
    n = errors = 0
    tmp = path.with_suffix(".tmp-values")
    with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        parts = {part: name for name, part in _sheet_parts(zin).items()}
        for item in zin.infolist():
            data = zin.read(item.filename)
            name = parts.get(item.filename)
            if name is not None and (b"<f>" in data or b"<f " in data):
                root = etree.fromstring(data)
                sheet_vals = values.get(name, {})
                for c in root.iter(f"{{{NS_MAIN}}}c"):
                    if c.find(f"{{{NS_MAIN}}}f") is not None:
                        n += 1
                        errors += _set_cached(c, sheet_vals.get(c.get("r")))
                data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            zout.writestr(item, data)
    os.replace(tmp, path)
    return n, errors


def embed_values(path: Path | str, timeout: int = 120) -> RecalcResult:
    """Calculate ``path`` with headless LibreOffice and embed the results. Never raises - returns a status."""
    path = Path(path)
    soffice = find_soffice()
    if not soffice:
        return RecalcResult(False, "LibreOffice not found - cached values were not embedded "
                                   "(Excel recalculates the workbook when it is opened).")
    try:
        with tempfile.TemporaryDirectory(prefix="ibkr_recalc_") as tmp_s:
            tmp = Path(tmp_s)
            src = tmp / "in.xlsx"
            shutil.copy2(path, src)
            out = tmp / "out"
            out.mkdir()
            cmd = [soffice, "--headless", "--norestore", "--nologo", "--nodefault",
                   f"-env:UserInstallation={(tmp / 'profile').as_uri()}",
                   "--convert-to", "xlsx", "--outdir", str(out), str(src)]
            try:
                subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
            except subprocess.TimeoutExpired:
                return RecalcResult(False, f"LibreOffice did not finish within {timeout}s - values not embedded.")
            result = out / "in.xlsx"
            if not result.exists():
                return RecalcResult(False, "LibreOffice produced no output - values not embedded.")
            values = _cell_values(result)
        n, errors = inject_values(path, values)
    except Exception as exc:                                    # noqa: BLE001 - optional step must never break export
        return RecalcResult(False, f"Could not embed calculated values: {exc!r}")
    msg = f"Embedded calculated values for {n} formula cells."
    if errors:
        msg += f" {errors} cell(s) evaluate to an Excel error - see the workbook."
    return RecalcResult(errors == 0, msg, n, errors)
