"""Reader for the IBKR *Activity Statement* HTML rendering.

The HTML is organised in ``<div id="sec…Heading">`` / ``<div id="tbl…Body">`` pairs.  Inside a body the
asset class and the currency are given by *sub-header rows* (``td.header-asset`` / ``td.header-currency``) -
this is where the transaction currency is taken from, exactly as for the CSV ``Currency`` column.

Everything is converted to the same row-dict shape the CSV reader produces (see :mod:`.raw`).
IBKR renders amounts rounded to 2 decimals in the HTML, hence ``precision="rounded"``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator, Optional

from bs4 import BeautifulSoup, NavigableString, Tag

from .raw import RawStatement


@dataclass
class HRow:
    kind: str                       # header | asset | currency | group | total | data
    cells: list[str]
    tr_classes: set[str] = field(default_factory=set)


def _text(cell: Tag) -> str:
    return re.sub(r"\s+", " ", cell.get_text(" ", strip=True).replace("\xa0", " ")).strip()


def _iter_rows(table: Tag) -> Iterator[HRow]:
    for tr in table.find_all("tr"):
        if tr.find_parent(class_=re.compile(r"row-detail")):          # collapsed per-lot detail
            continue
        cells = tr.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        classes = set(tr.get("class") or [])
        texts = [_text(c) for c in cells]
        first_cls = set(cells[0].get("class") or [])
        if all(c.name == "th" for c in cells):
            kind = "header"
        elif "header-asset" in first_cls:
            kind = "asset"
        elif "header-currency" in first_cls:
            kind = "currency"
        elif classes & {"subtotal", "total"}:
            kind = "total"
        elif len(cells) == 1 and texts[0]:
            kind = "group"
        else:
            kind = "data"
        yield HRow(kind, texts, classes)


def _unique_header(cells: list[str]) -> list[str]:
    out: list[str] = []
    for i, c in enumerate(cells):
        key = c or f"_{i}"
        if key in out:
            key = f"{key}_{i}"
        out.append(key)
    return out


def _is_lot_row(r: HRow) -> bool:
    return any("lot" in c.lower() for c in r.tr_classes)


# --------------------------------------------------------------------------------------
# section parsers
# --------------------------------------------------------------------------------------
def _parse_generic(table: Tag, defaults: Optional[dict[str, str]] = None, groups_are_currency: bool = False,
                   first_col: Optional[str] = None) -> list[dict[str, str]]:
    header: Optional[list[str]] = None
    asset = ccy = ""
    out: list[dict[str, str]] = []
    for r in _iter_rows(table):
        if r.kind == "header":
            header = _unique_header(r.cells)
            if first_col and header and header[0].startswith("_"):
                header[0] = first_col
            continue
        if r.kind == "asset":
            asset = r.cells[0]
            continue
        if r.kind == "currency":
            ccy = r.cells[0]
            continue
        if r.kind == "group":
            if groups_are_currency:
                ccy = r.cells[0]
            continue
        if r.kind == "total" or header is None or _is_lot_row(r):
            continue
        row = dict(zip(header, r.cells))
        if defaults:
            for k, v in defaults.items():
                row.setdefault(k, v)
        row.setdefault("Asset Category", asset)
        row.setdefault("Subtitle", asset)
        row.setdefault("Currency", ccy)
        out.append(row)
    return out


def _parse_nav(div: Tag) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    tables = div.find_all("table")
    if not tables:
        return rows
    for r in _iter_rows(tables[0]):
        if r.kind == "header" or not r.cells:
            continue
        label = r.cells[0].strip()
        if label.lower().startswith("time weighted"):
            rows.append({"Time Weighted Rate of Return": r.cells[-1]})
        elif label and len(r.cells) >= 5:
            rows.append({"Asset Class": label, "Current Total": r.cells[4]})
    return rows


def _parse_mtm(table: Tag) -> list[dict[str, str]]:
    asset = ""
    out: list[dict[str, str]] = []
    for r in _iter_rows(table):
        if r.kind in ("group", "asset"):
            asset = r.cells[0]
        elif r.kind == "data" and len(r.cells) >= 6:
            out.append({
                "Asset Category": asset, "Symbol": r.cells[0],
                "Prior Quantity": r.cells[1], "Current Quantity": r.cells[2],
                "Prior Price": r.cells[3], "Current Price": r.cells[4],
            })
    return out


def _parse_key_value(table: Tag) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in _iter_rows(table):
        if r.kind in ("data", "total") and len(r.cells) >= 2:
            out[r.cells[0].strip().lower()] = r.cells[1].strip()
    return out


# --------------------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------------------
def _heading_text(h: Tag) -> str:
    direct = "".join(str(s) for s in h.children if isinstance(s, NavigableString)).strip()
    if direct:
        return re.sub(r"\s+", " ", direct)
    return re.sub(r"\s+", " ", h.get_text(" ", strip=True).split("|")[0]).strip()


def read_html(data: bytes | str) -> RawStatement:
    soup = BeautifulSoup(data, "lxml")
    sections: dict[str, list[dict[str, str]]] = {}
    meta: dict[str, str] = {}

    for heading in soup.find_all("div", id=re.compile(r"^sec.+Heading$")):
        name = _heading_text(heading)
        body = soup.find(id="tbl" + heading["id"][3:-len("Heading")] + "Body")
        if body is None:
            continue
        table = body.find("table")
        if table is None:
            continue
        if name == "Account Information":
            meta.update(_parse_key_value(table))
        elif name == "Net Asset Value":
            sections[name] = _parse_nav(body)
        elif name == "Mark-to-Market Performance Summary":
            sections[name] = _parse_mtm(table)
        elif name == "Cash Report":
            sections[name] = _parse_generic(table, groups_are_currency=True, first_col="Currency Summary")
        elif name == "Trades":
            sections[name] = _parse_generic(table, defaults={"DataDiscriminator": "Order"})
        elif name == "Open Positions":
            sections[name] = _parse_generic(table, defaults={"DataDiscriminator": "Summary"})
        elif name in ("Codes", "Notes/Legal Notes", "Realized & Unrealized Performance Summary"):
            continue
        else:
            sections[name] = _parse_generic(table)

    # statement level information
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    account = meta.get("account", "")
    if not account:
        m = re.match(r"\s*([A-Z]{1,2}\d{5,})\b", title)
        account = m.group(1) if m else ""
    period = ""
    title_p = soup.find("p", class_="text-title")
    if title_p is not None and title_p.find("span") is not None:
        period = _text(title_p.find("span"))
    if not period:
        m = re.search(r"Activity Statement\s+(.+?)\s+-\s+Interactive Brokers", title)
        period = m.group(1) if m else ""
    generated = ""
    m = re.search(r"Generated:\s*([0-9]{4}-[0-9]{2}-[0-9]{2},\s*[0-9:]{8}\s*[A-Z]{2,4})", str(soup))
    if m:
        generated = m.group(1)

    return RawStatement(
        fmt="html",
        precision="rounded",
        meta={
            "account": account,
            "alias": meta.get("account alias", ""),
            "holder": meta.get("name", ""),
            "base_currency": meta.get("base currency", ""),
            "period": period,
            "generated": generated,
            "broker": "",
        },
        sections=sections,
    )
