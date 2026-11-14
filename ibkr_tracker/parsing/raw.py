"""Format-neutral intermediate representation produced by the CSV and the HTML readers.

Both readers emit the *same* shape - ``sections[name] = [row-dict, ...]`` with the column names used by the
IBKR CSV export - so a single extraction routine (:mod:`ibkr_tracker.parsing.extract`) builds the data model.
That is what guarantees CSV and HTML uploads produce identical results (up to the 2-decimals rounding that
IBKR applies to amounts in the HTML rendering).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RawStatement:
    fmt: str                                   # "csv" | "html"
    precision: str                             # "full" | "rounded"
    meta: dict[str, str] = field(default_factory=dict)
    sections: dict[str, list[dict[str, str]]] = field(default_factory=dict)
