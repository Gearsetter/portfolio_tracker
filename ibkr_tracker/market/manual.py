"""Manual price history: read ``Date, Price`` data that the user typed, pasted or uploaded.

The standard format is one price per line, a date and a closing price::

    Date,Price
    2026-01-02,125.40
    2026-01-05,127.15

Everything around it is tolerated, because the data usually comes from a broker portal, a bank's web page or an
Excel sheet and not from a program:

* separators: comma, semicolon, tab, ``|`` or blanks; a header line is optional (``Date;Close``, ``Datum;Kurs`` ...);
  a Yahoo-style table (``Date,Open,High,Low,Close,Adj Close,Volume``) is accepted too - the ``Price`` / ``Close``
  column is used;
* dates: ``2026-01-02``, ``2026/01/02``, ``2026.01.02.``, ``20260102``, ``02.01.2026``, ``01/02/2026`` (month first
  with slashes unless the data prove otherwise), ``2 Jan 2026``, ``Jan 2, 2026``, ISO timestamps (the time is
  ignored) and Excel serial numbers;
* prices: decimal point or decimal comma, thousands separators, currency signs (``$``, ``EUR``, ``Ft``) are ignored.

Nothing is guessed silently: the result lists every interpretation that was made (separator, decimal mark, day
order, the column used) and every line that had to be skipped, so that the GUI can show a preview before anything is
saved.  Prices are in the **trading currency of the stock** and - like Yahoo's ``Close`` - split-adjusted.
"""
from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional, Sequence

BENCHMARK_KEY = "@benchmark"           # storage key of the manual price history of the benchmark ETF
MAX_ROWS = 200_000
MAX_MESSAGES = 25                      # longest list of per-line problems that is kept
JUMP_RATIO = 1.5                       # a day-to-day move beyond x1.5 / below /1.5 is reported as suspicious
EARLIEST = date(1970, 1, 1)

_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), 1)}
_DATE_HEADERS = ("date", "datum", "day", "time", "timestamp", "trade date", "as of", "asof", "dátum", "datetime")
_PRICE_HEADERS = ("price", "close", "closing price", "close price", "last", "last price", "kurs", "ár",
                  "zárás", "adj close", "adj. close", "adjusted close", "value", "nav", "settle")
_TIME_TAIL = re.compile(r"[T\s,]+\d{1,2}:\d{2}.*$")
_ISO = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\.?$")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_NUMERIC_DATE = re.compile(r"^(\d{1,2})([-/.])(\d{1,2})[-/.](\d{4})\.?$")
_DMON = re.compile(r"^(\d{1,2})[-/. ]+([A-Za-z]{3,9})\.?[-/., ]+(\d{4})$")
_MONDY = re.compile(r"^([A-Za-z]{3,9})\.?[-/. ]+(\d{1,2})(?:st|nd|rd|th)?[-/., ]+(\d{4})$")
_SERIAL = re.compile(r"^\d{5}(?:\.\d+)?$")


class PriceParseError(ValueError):
    """The text cannot be read at all (empty, binary ...)."""


@dataclass
class ParsedPrices:
    """Outcome of :func:`parse_prices`: the valid rows plus everything the user should know about."""
    rows: list[tuple[date, float]] = field(default_factory=list)       # ascending, one price per day
    errors: list[str] = field(default_factory=list)                    # skipped lines (with line number and reason)
    warnings: list[str] = field(default_factory=list)                  # interpretations and suspicious values
    n_errors: int = 0                                                  # total skipped lines (errors is truncated)
    delimiter: str = ""
    decimal: str = "."
    date_order: str = ""                                               # "dmy" | "mdy" | "" (no ambiguous dates)
    header: bool = False
    price_column: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.rows)

    @property
    def clean(self) -> bool:
        return bool(self.rows) and not self.n_errors

    @property
    def first(self) -> Optional[date]:
        return self.rows[0][0] if self.rows else None

    @property
    def last(self) -> Optional[date]:
        return self.rows[-1][0] if self.rows else None

    def describe(self) -> str:
        """One line for the preview: what was understood."""
        if not self.rows:
            return "No price rows found."
        sep = {"\t": "tab", ";": "semicolon", ",": "comma", "|": "pipe", " ": "blank"}.get(self.delimiter, "?")
        parts = [f"{len(self.rows)} price(s) from {self.first} to {self.last}",
                 f"separator: {sep}", f"decimal mark: '{self.decimal}'"]
        if self.date_order:
            parts.append("dates: day first" if self.date_order == "dmy" else "dates: month first")
        if self.price_column:
            parts.append(f"price column: {self.price_column}")
        return " · ".join(parts)


# ---------------------------------------------------------------------------------------------------------------
# text handling
# ---------------------------------------------------------------------------------------------------------------
def decode_bytes(data: bytes) -> str:
    """Text of an uploaded file: UTF-8 (with or without BOM), UTF-16 or - as the last resort - Windows-1252."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if b"\x00" in data[:200]:
        raise PriceParseError("This looks like a binary file (an .xlsx workbook?). Save it as CSV first, or paste "
                              "the two columns (date and price) as text.")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _clean_lines(text: str) -> list[tuple[int, str]]:
    out = []
    for n, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        line = raw.replace("\xa0", " ").strip()
        if line and not line.startswith("#"):
            out.append((n, line))
    return out


_BLANK_DATE = re.compile(
    r"^(?P<d>\d{1,2}[-/. ]+[A-Za-z]{3,9}\.?,?[-/. ]+\d{4}"                      # 2 Jan 2026
    r"|[A-Za-z]{3,9}\.?[-/. ]+\d{1,2}(?:st|nd|rd|th)?,?[-/. ]+\d{4}"             # Jan 2, 2026
    r"|\S+(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)\s+(?P<rest>\S.*)$")                # 2026-01-02 [16:00:00]


def _split_blank(line: str) -> list[str]:
    """Blank separated line: the date may consist of several words (``Jan 2, 2026``) or carry a time."""
    m = _BLANK_DATE.match(line)
    if not m:
        return line.split()
    return [m.group("d").strip(), *m.group("rest").split()]


def _split(line: str, delimiter: str) -> list[str]:
    if delimiter == " ":
        return _split_blank(line)
    try:
        row = next(csv.reader([line], delimiter=delimiter, quotechar='"', skipinitialspace=True))
    except (csv.Error, StopIteration):
        row = line.split(delimiter)
    return [c.strip().strip('"').strip() for c in row]


def detect_delimiter(lines: Sequence[str]) -> str:
    """The separator of the data: tab, ``;``, ``|``, ``,`` (when at least half of the lines contain it *and* the
    lines then start with a date) - otherwise blanks."""
    if not lines:
        return ","
    need = max(1, math.ceil(len(lines) / 2))
    present = [c for c in ("\t", ";", "|", ",") if sum(1 for l in lines if c in l) >= need]
    sample = list(lines[:12])
    for cand in (*present, " "):
        ok = 0
        for l in sample:
            f = _split(l, cand)
            ok += len(f) >= 2 and is_date(f[0])
        if ok >= max(1, len(sample) // 2):
            return cand
    return present[0] if present else " "


# ---------------------------------------------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------------------------------------------
def _strip_date(text: str) -> str:
    t = text.strip().strip('"').strip()
    t = _TIME_TAIL.sub("", t)
    return re.sub(r"\.\s+", ".", t).strip()


def _make(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def date_order_evidence(tokens: Sequence[str]) -> tuple[bool, bool, set[str]]:
    """``(day_first_proof, month_first_proof, separators)`` over all ``a<sep>b<sep>yyyy`` dates: a first number
    above 12 proves day first, a second one above 12 proves month first."""
    dmy = mdy = False
    seps: set[str] = set()
    for tok in tokens:
        m = _NUMERIC_DATE.match(_strip_date(tok))
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(3))
        seps.add(m.group(2))
        dmy |= a > 12 >= b
        mdy |= b > 12 >= a
    return dmy, mdy, seps


def parse_day(text: str, order: str = "") -> Optional[date]:
    """One date.  ``order`` ("dmy"/"mdy") resolves ``01/02/2026``-style dates; without it ``.`` and ``-`` mean day
    first and ``/`` means month first.  Returns ``None`` when the text is not a date."""
    t = _strip_date(text)
    if not t:
        return None
    m = _ISO.match(t)
    if m:
        return _make(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _COMPACT.match(t)
    if m:
        return _make(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _NUMERIC_DATE.match(t)
    if m:
        a, sep, b, y = int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4))
        o = order or ("mdy" if sep == "/" else "dmy")
        return _make(y, b, a) if o == "dmy" else _make(y, a, b)
    m = _DMON.match(t)
    if m and m.group(2)[:3].lower() in _MONTHS:
        return _make(int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
    m = _MONDY.match(t)
    if m and m.group(1)[:3].lower() in _MONTHS:
        return _make(int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2)))
    if _SERIAL.match(t):                                       # Excel serial day number (1900 date system)
        n = float(t)
        if 20000 < n < 80000:
            return date(1899, 12, 30) + timedelta(days=int(n))
    return None


def is_date(text: str) -> bool:
    """True when ``text`` is a date in *some* day order: ``13/01/2026`` is a day-first date although a slash usually
    means month first, so the header / delimiter detection must not judge it by the default order alone."""
    return any(parse_day(text, order) is not None for order in ("", "dmy", "mdy"))


# ---------------------------------------------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------------------------------------------
_NUM_CHARS = re.compile(r"[^0-9.,\-+()]")


def _numeric_core(text: str) -> str:
    return _NUM_CHARS.sub("", text.replace("\xa0", " ").replace("−", "-"))


def detect_decimal(tokens: Sequence[str], delimiter: str = ",") -> str:
    """Decimal mark of the price column: ``"."`` or ``","`` (``"1,234.50"`` and ``"1.234,50"`` are unambiguous;
    a lone ``"12,5"`` is a decimal comma; ``"1,234"`` alone is read as thousands unless the file is
    semicolon separated - the dialect of locales that use a decimal comma)."""
    comma = dot = 0
    for tok in tokens:
        c = _numeric_core(tok).strip("()+-")
        if "," in c and "." in c:
            if c.rfind(",") > c.rfind("."):
                comma += 1
            else:
                dot += 1
        elif "," in c:
            if c.count(",") > 1:
                continue                                       # 1,234,567 - thousands
            head, tail = c.split(",")
            if len(tail) != 3 or head in ("", "0"):
                comma += 1                                     # 12,5   12,50   0,125   12,12345
        elif "." in c:
            if c.count(".") > 1:
                comma += 1                                     # 1.234.567 - thousands of a decimal-comma file
                continue
            head, tail = c.split(".")
            if len(tail) != 3 or head in ("", "0"):
                dot += 1
    if comma > dot:
        return ","
    if dot > comma:
        return "."
    return "," if delimiter == ";" else "."


def parse_price(text: str, decimal: str = ".") -> Optional[float]:
    """One price (currency signs, spaces and thousands separators ignored); ``None`` when it is not a number."""
    c = _numeric_core(text)
    if not c or not re.search(r"\d", c):
        return None
    neg = (c.startswith("(") and c.endswith(")")) or c.startswith("-")
    c = c.strip("()+-")
    if decimal == ",":
        c = c.replace(".", "").replace(",", ".")
    else:
        c = c.replace(",", "")
    try:
        v = float(c)
    except ValueError:
        return None
    return -v if neg else v


# ---------------------------------------------------------------------------------------------------------------
# the parser
# ---------------------------------------------------------------------------------------------------------------
def _find_column(header: Sequence[str], names: Sequence[str]) -> Optional[int]:
    low = [h.strip().lower() for h in header]
    for name in names:                                         # priority order of ``names``
        if name in low:
            return low.index(name)
    return None


def parse_prices(text: str, *, decimal: Optional[str] = None, date_order: Optional[str] = None,
                 max_day: Optional[date] = None) -> ParsedPrices:
    """Parse pasted / uploaded ``Date, Price`` text (see the module docstring).

    ``decimal`` (``"."`` / ``","``) and ``date_order`` (``"dmy"`` / ``"mdy"``) override the automatic detection;
    ``max_day`` (default: tomorrow) is the latest date accepted - a price from the future is a typing error.
    Raises :class:`PriceParseError` only when there is nothing to read at all."""
    lines = _clean_lines(text)
    if not lines:
        raise PriceParseError("Nothing to read - paste lines such as '2026-01-02, 125.40'.")
    if len(lines) > MAX_ROWS:
        raise PriceParseError(f"Too many lines ({len(lines):,}); the limit is {MAX_ROWS:,}.")
    res = ParsedPrices()
    max_day = max_day or (date.today() + timedelta(days=1))
    res.delimiter = detect_delimiter([l for _, l in lines])
    table = [(n, _split(l, res.delimiter)) for n, l in lines]

    # ---- header -----------------------------------------------------------------------------------------------
    first_n, first = table[0]
    date_col, price_col = 0, 1
    has_header = len(first) >= 1 and not is_date(first[0])
    if has_header and len(first) < 2:
        raise PriceParseError(f"Expected two columns - a date and a price per line (found only '{first[0]}'). "
                              "Example: 2026-01-02, 125.40")
    if has_header:
        res.header = True
        d = _find_column(first, _DATE_HEADERS)
        p = _find_column(first, _PRICE_HEADERS)
        date_col = d if d is not None else 0
        if p is not None:
            price_col = p
        elif len(first) == 2 or (len(first) >= 2 and not any(re.search(r"[a-z]", h.lower()) for h in first[1:])):
            price_col = 1 if date_col == 0 else 0
        else:
            raise PriceParseError("Could not find the price column in the header line "
                                  f"({', '.join(first)}). Name it 'Price' or 'Close', or use just two columns: "
                                  "Date, Price.")
        res.price_column = first[price_col] if price_col < len(first) else ""
        table = table[1:]
    elif len(first) > 2:
        res.price_column = "column 2"
        hint = ""
        if res.delimiter == "," and len(first) == 3 and re.fullmatch(r"\d+", first[2] or ""):
            hint = (" If the prices use a decimal comma (12,5), separate the columns with a semicolon or a tab "
                    "instead.")
        res.warnings.append(f"The lines have {len(first)} columns and no header - the second column was used as the "
                            f"price.{hint}")
    if not table:
        raise PriceParseError("Only a header line was found - there are no prices below it.")

    # ---- interpretation of dates and numbers (decided over the whole file) ----------------------------------------
    date_tokens = [r[date_col] for _, r in table if len(r) > date_col]
    price_tokens = [r[price_col] for _, r in table if len(r) > price_col]
    dmy, mdy, _ = date_order_evidence(date_tokens)
    if date_order in ("dmy", "mdy"):
        res.date_order = date_order
    elif dmy and mdy:
        res.date_order = ""
        res.warnings.append("Some dates are day first, others month first - check the file.")
    elif dmy or mdy:
        res.date_order = "dmy" if dmy else "mdy"
    else:
        ambiguous = [t for t in date_tokens if (m := _NUMERIC_DATE.match(_strip_date(t)))
                     and int(m.group(1)) <= 12 and int(m.group(3)) <= 12 and int(m.group(1)) != int(m.group(3))]
        if ambiguous:
            seps = {_NUMERIC_DATE.match(_strip_date(t)).group(2) for t in ambiguous}   # type: ignore[union-attr]
            res.date_order = "mdy" if seps == {"/"} else "dmy"
            res.warnings.append(
                f"Dates such as {ambiguous[0]} could be day-first or month-first - read as "
                f"{'month first (MM/DD/YYYY)' if res.date_order == 'mdy' else 'day first (DD.MM.YYYY)'}. "
                "Choose the other order below if that is wrong.")
    res.decimal = decimal if decimal in (".", ",") else detect_decimal(price_tokens, res.delimiter)
    if res.delimiter == "," and res.decimal == ",":
        res.warnings.append("A decimal comma in a comma separated file is ambiguous - separate the columns with a "
                            "semicolon or a tab, or quote the numbers.")

    # ---- rows -------------------------------------------------------------------------------------------------
    by_day: dict[date, float] = {}
    duplicates = 0

    def bad(n: int, msg: str) -> None:
        res.n_errors += 1
        if len(res.errors) < MAX_MESSAGES:
            res.errors.append(f"line {n}: {msg}")

    for n, row in table:
        if len(row) <= max(date_col, price_col):
            bad(n, f"expected 'Date, Price', found '{', '.join(row)}'")
            continue
        day = parse_day(row[date_col], res.date_order)
        if day is None:
            bad(n, f"'{row[date_col]}' is not a date")
            continue
        price = parse_price(row[price_col], res.decimal)
        if price is None or not math.isfinite(price):
            bad(n, f"'{row[price_col]}' is not a price")
            continue
        if price <= 0:
            bad(n, f"the price on {day} must be above zero (found {row[price_col]})")
            continue
        if day > max_day:
            bad(n, f"{day} is in the future")
            continue
        if day < EARLIEST:
            bad(n, f"{day} is before {EARLIEST.year}")
            continue
        if day in by_day:
            duplicates += 1
        by_day[day] = price
    res.rows = sorted(by_day.items())
    if res.n_errors > len(res.errors):
        res.errors.append(f"... and {res.n_errors - len(res.errors)} more line(s) were skipped")
    if duplicates:
        res.warnings.append(f"{duplicates} date(s) occur more than once - the last value of each was kept.")
    for (d0, p0), (d1, p1) in zip(res.rows, res.rows[1:]):
        if p1 / p0 > JUMP_RATIO or p0 / p1 > JUMP_RATIO:
            res.warnings.append(f"Large jump from {p0:g} on {d0} to {p1:g} on {d1} - a typing error, or prices that "
                                "are not split-adjusted?")
            if len(res.warnings) >= MAX_MESSAGES:
                break
    return res


def parse_price_file(data: bytes, **kw) -> ParsedPrices:
    """:func:`parse_prices` for the bytes of an uploaded CSV / text file."""
    return parse_prices(decode_bytes(data), **kw)


# ---------------------------------------------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------------------------------------------
def prices_to_text(rows: Sequence[tuple[date, float]], sep: str = ",") -> str:
    """The standard ``Date,Price`` text of a series (what the download button hands out)."""
    out = io.StringIO()
    out.write(f"Date{sep}Price\n")
    for d, v in rows:
        out.write(f"{d.isoformat()}{sep}{v:.10g}\n")
    return out.getvalue()


def make_key(ticker: str, currency: str) -> str:
    return f"{ticker}|{currency}"


def parse_key(key: str) -> tuple[str, str]:
    t, _, c = key.partition("|")
    return t, c
