"""Reader for the IBKR *Activity Statement* CSV export.

The CSV is a sequence of ``Section,RowType,field...`` records.  A ``Header`` row defines the columns of the
``Data`` rows that follow it; a section may define several headers (``Trades`` has one for stocks and a
different one - with ``Comm in HUF`` - for forex), so the active header is tracked per section while reading.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict

from .raw import RawStatement


def decode_bytes(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_csv(data: bytes | str) -> RawStatement:
    text = decode_bytes(data) if isinstance(data, (bytes, bytearray)) else data
    sections: dict[str, list[dict[str, str]]] = defaultdict(list)
    headers: dict[str, list[str]] = {}
    meta: dict[str, str] = {}

    for rec in csv.reader(io.StringIO(text)):
        if len(rec) < 2:
            continue
        section, kind = rec[0].strip(), rec[1].strip()
        rest = rec[2:]

        if kind == "Header":
            headers[section] = [h.strip() for h in rest]
            continue
        if kind != "Data":                       # SubTotal / Total / Notes rows carry no new information
            continue

        if section in ("Statement", "Account Information") and len(rest) >= 2:
            meta[rest[0].strip().lower()] = rest[1].strip()
            continue

        hdr = headers.get(section)
        if not hdr:
            continue
        row: dict[str, str] = {}
        for i, name in enumerate(hdr):
            key = name or f"_{i}"
            if key in row:                       # duplicated column names -> keep the first, suffix the rest
                key = f"{key}_{i}"
            row[key] = rest[i].strip() if i < len(rest) else ""
        sections[section].append(row)

    return RawStatement(
        fmt="csv",
        precision="full",
        meta={
            "account": meta.get("account", ""),
            "alias": meta.get("account alias", ""),
            "holder": meta.get("name", ""),
            "base_currency": meta.get("base currency", ""),
            "period": meta.get("period", ""),
            "generated": meta.get("whengenerated", ""),
            "broker": meta.get("brokername", ""),
        },
        sections=dict(sections),
    )
