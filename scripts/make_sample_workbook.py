"""Regenerate ``examples/output/IBKR_Portfolio_<account>_sample.xlsx`` from the sample statements.

Works completely offline: prices and FX come from the statements themselves (the VUAA benchmark columns stay
empty until *Refresh Prices* has downloaded data).  When the statements belong to more than one account, the
consolidated (all accounts merged) workbook ``IBKR_Portfolio_ALL_sample.xlsx`` is written as well.  Usage::

    python scripts/make_sample_workbook.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ibkr_tracker.excel import find_soffice                        # noqa: E402
from ibkr_tracker.portfolio import CONSOLIDATED                    # noqa: E402
from ibkr_tracker.storage import Store                             # noqa: E402
from ibkr_tracker.tracker import Tracker                           # noqa: E402


def main() -> int:
    statements = sorted((ROOT / "examples" / "statements").iterdir())
    target_dir = ROOT / "examples" / "output"
    target_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ibkr_sample_") as tmp:
        t = Tracker(Store(Path(tmp) / "tracker.db"))
        t.update_settings(price_source="statement", output_dir=tmp, recalc_with_libreoffice=True, export_excel=True)
        accounts = set()
        for f in statements:
            res = t.import_statement(f.name, f.read_bytes())
            print(res.message)
            accounts.add(res.account)
        real = sorted(a for a in accounts if a)
        for account in [*real, *([CONSOLIDATED] if len(real) > 1 else [])]:
            out = t.export(account)
            for e in out.errors:
                print("ERROR:", e)
            if out.update is None:
                return 1
            dest = target_dir / f"{out.update.path.stem}_sample.xlsx"
            shutil.copy2(out.update.path, dest)
            print(f"{out.update.action}: {dest}")
            if out.update.recalc is not None:
                print("  ", out.update.recalc.message)
    if find_soffice() is None:
        print("LibreOffice not found: the file has formulas but no cached values (Excel calculates them on open).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
