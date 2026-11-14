"""Statement parsing entry point: detects CSV vs HTML and returns a :class:`~ibkr_tracker.models.ParsedStatement`."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Union

from ..models import ParsedStatement
from .csv_reader import decode_bytes, read_csv
from .extract import build_statement
from .html_reader import read_html

__all__ = ["parse_statement", "parse_statement_file", "detect_format"]


def detect_format(filename: str, data: bytes) -> str:
    """``csv`` or ``html`` - by extension first, then by sniffing the content."""
    suffix = Path(filename).suffix.lower()
    if suffix in (".htm", ".html"):
        return "html"
    if suffix == ".csv":
        return "csv"
    head = data[:2000].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<html" in head:
        return "html"
    return "csv"


def parse_statement(filename: str, data: bytes) -> ParsedStatement:
    """Parse one uploaded IBKR Activity Statement (``.csv`` or ``.htm``/``.html``)."""
    fmt = detect_format(filename, data)
    sha = hashlib.sha256(data).hexdigest()
    raw = read_html(data) if fmt == "html" else read_csv(data)
    ps = build_statement(raw, Path(filename).name, sha)
    if not (ps.trades or ps.fx_trades or ps.cash_flows or ps.div_lines or ps.open_positions or ps.cash_report):
        ps.warnings.append("No transactions or positions were found - is this an IBKR Activity Statement?")
    return ps


def parse_statement_file(path: Union[str, Path]) -> ParsedStatement:
    p = Path(path)
    return parse_statement(p.name, p.read_bytes())
