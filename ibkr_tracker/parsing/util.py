"""Small parsing helpers shared by the CSV and HTML readers."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

# --------------------------------------------------------------------------------------
# numbers
# --------------------------------------------------------------------------------------
_EMPTY = {"", "-", "--", "---", "n/a", "na", "none", "null", "—", "–"}


def to_num(value: Any) -> Optional[float]:
    """Parse IBKR numbers: ``"1,224"``, ``"-2,421.86"``, ``"(5.0)"``, ``"19.2%"`` -> float; blanks -> None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("\xa0", " ").strip()
    if text.lower() in _EMPTY:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()").replace(",", "").replace(" ", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def num0(value: Any) -> float:
    """Like :func:`to_num` but blanks become 0.0."""
    n = to_num(value)
    return 0.0 if n is None else n


# --------------------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------------------
_DT_FORMATS = (
    "%Y-%m-%d, %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d;%H:%M:%S",
    "%Y%m%d;%H%M%S",
    "%Y%m%d %H%M%S",
    "%Y-%m-%d, %H:%M",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y%m%d",
)


def parse_dt(value: Any) -> Optional[datetime]:
    """Parse ``2026-02-12, 11:47:53`` (also date-only values) into a naive datetime (Eastern Time as in IBKR)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).replace("\xa0", " ").strip().strip('"')
    if not text:
        return None
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_date(value: Any) -> Optional[date]:
    dt = parse_dt(value)
    return dt.date() if dt else None


def parse_period(text: str) -> tuple[Optional[date], Optional[date]]:
    """``"January 1, 2026 - October 23, 2026"`` -> (date, date); a single day returns it twice."""
    if not text:
        return None, None
    parts = [p.strip() for p in re.split(r"\s+-\s+|\s+–\s+", text.strip()) if p.strip()]

    def one(s: str) -> Optional[date]:
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        return None

    if len(parts) == 1:
        d = one(parts[0])
        return d, d
    return one(parts[0]), one(parts[-1])


# --------------------------------------------------------------------------------------
# descriptions of dividend related lines
# --------------------------------------------------------------------------------------
_ISIN_DESC = re.compile(r"^\s*(?P<sym>.+?)\s*\((?P<isin>[A-Z]{2}[A-Z0-9]{9}\d)\)\s*(?P<rest>.*)$", re.S)
_RATE = re.compile(
    r"(?:cash\s+dividend|payment\s+in\s+lieu\s+of\s+dividend|dividend)\s+([A-Z]{3})\s+([0-9]*\.?[0-9]+)",
    re.I,
)


def split_description(desc: str) -> tuple[str, str, str]:
    """``"BN (CA11271J1075) Cash Dividend USD 0.07 (Ordinary Dividend)"`` -> ("BN", "CA11271J1075", rest)."""
    m = _ISIN_DESC.match(desc or "")
    if not m:
        return "", "", (desc or "").strip()
    return m.group("sym").strip(), m.group("isin"), m.group("rest").strip()


def description_key(desc: str) -> str:
    """Normalised text identifying the dividend event a tax / fee line belongs to.

    ``... per Share (Ordinary Dividend)``, ``... per Share - US Tax`` and ``... per Share - FEE`` all map to
    the same key, which is how dividends, withholding taxes and fees are synchronised.
    """
    _, _, rest = split_description(desc)
    rest = re.sub(r"\s+", " ", rest).strip()
    rest = re.sub(r"\s*-\s*(?:[A-Za-z]{2,}\s+)?(?:tax|fee)s?\s*$", "", rest, flags=re.I)
    rest = re.sub(r"\s*\([^()]*\)\s*$", "", rest)
    rest = re.sub(r"\s*-\s*(?:[A-Za-z]{2,}\s+)?(?:tax|fee)s?\s*$", "", rest, flags=re.I)
    return rest.strip().lower()


def parse_rate(desc: str) -> Optional[tuple[str, float]]:
    """Per-share rate quoted in a dividend description: ("USD", 0.91)."""
    m = _RATE.search(desc or "")
    if not m:
        return None
    return m.group(1).upper(), float(m.group(2))


def is_dividend_related(desc: str) -> bool:
    sym, isin, rest = split_description(desc)
    return bool(sym) and bool(re.search(r"dividend", rest, re.I))


# --------------------------------------------------------------------------------------
# dict helpers
# --------------------------------------------------------------------------------------
def pick(row: dict[str, Any], *names: str, default: Any = "") -> Any:
    """Return the first non-empty value among alias column names (case-insensitive)."""
    lowered = {k.lower(): v for k, v in row.items() if isinstance(k, str)}
    for n in names:
        v = lowered.get(n.lower())
        if v is not None and str(v).strip() != "":
            return v
    return default
