"""SQLite persistence: statement archive, market-data cache and session settings.

Everything the app learns survives restarts: every imported statement (as parsed JSON, de-duplicated by
file hash), every downloaded price / FX series (incrementally extended on refresh) and the user's settings.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Optional, Sequence

from .config import DEFAULT_SETTINGS, data_dir
from .models import ParsedStatement, StatementInfo, statement_from_json, statement_to_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS statements(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT UNIQUE NOT NULL,
    filename TEXT, fmt TEXT, account TEXT, alias TEXT, base_currency TEXT,
    period_start TEXT, period_end TEXT, imported_at TEXT,
    n_trades INTEGER, n_other INTEGER,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prices(
    symbol TEXT NOT NULL, day TEXT NOT NULL, close REAL NOT NULL,
    PRIMARY KEY(symbol, day)
);
CREATE TABLE IF NOT EXISTS price_meta(
    symbol TEXT PRIMARY KEY, currency TEXT, name TEXT, fetched_at TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS manual_prices(
    key TEXT NOT NULL, day TEXT NOT NULL, price REAL NOT NULL, source TEXT, imported_at TEXT,
    PRIMARY KEY(key, day)
);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS snapshots(
    account TEXT NOT NULL, as_of TEXT NOT NULL, taken_at TEXT NOT NULL,
    usd_total REAL, base_total REAL, usd_deposits REAL, base_deposits REAL, stocks_usd REAL, cash_usd REAL,
    base_currency TEXT,
    PRIMARY KEY(account, as_of)
);
"""


class Store:
    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path else data_dir() / "tracker.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ---- statements -------------------------------------------------------------------------------------
    def add_statement(self, ps: ParsedStatement) -> tuple[int, bool]:
        """Archive a parsed statement. Returns ``(id, is_new)``; re-uploading the same file is a no-op."""
        with self._conn() as c:
            row = c.execute("SELECT id FROM statements WHERE sha256=?", (ps.sha256,)).fetchone()
            if row:
                return int(row["id"]), False
            n_trades = len(ps.trades)
            n_other = len(ps.fx_trades) + len(ps.cash_flows) + len(ps.div_lines)
            cur = c.execute(
                "INSERT INTO statements(sha256, filename, fmt, account, alias, base_currency, period_start, "
                "period_end, imported_at, n_trades, n_other, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (ps.sha256, ps.filename, ps.fmt, ps.account, ps.alias, ps.base_currency,
                 ps.period_start.isoformat() if ps.period_start else None,
                 ps.period_end.isoformat() if ps.period_end else None,
                 datetime.now().isoformat(timespec="seconds"), n_trades, n_other, statement_to_json(ps)))
            return int(cur.lastrowid), True

    def list_statements(self, account: Optional[str] = None) -> list[StatementInfo]:
        q = ("SELECT id, filename, fmt, account, alias, period_start, period_end, imported_at, n_trades, n_other "
             "FROM statements")
        args: tuple = ()
        if account:
            q += " WHERE account=?"
            args = (account,)
        q += " ORDER BY period_end, period_start, id"
        with self._conn() as c:
            rows = c.execute(q, args).fetchall()
        return [StatementInfo(
            id=r["id"], filename=r["filename"], fmt=r["fmt"], account=r["account"], alias=r["alias"] or "",
            period_start=date.fromisoformat(r["period_start"]) if r["period_start"] else None,
            period_end=date.fromisoformat(r["period_end"]) if r["period_end"] else None,
            imported_at=r["imported_at"], n_trades=r["n_trades"], n_other=r["n_other"]) for r in rows]

    def delete_statement(self, statement_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM statements WHERE id=?", (statement_id,))

    def load_statements(self, account: Optional[str] = None) -> list[ParsedStatement]:
        q = "SELECT payload FROM statements"
        args: tuple = ()
        if account:
            q += " WHERE account=?"
            args = (account,)
        q += " ORDER BY period_end, period_start, id"
        with self._conn() as c:
            return [statement_from_json(r["payload"]) for r in c.execute(q, args).fetchall()]

    def accounts(self) -> list[tuple[str, str, Optional[date]]]:
        """[(account, alias, latest period end)] - most recently covered account first."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT account, MAX(alias) AS alias, MAX(period_end) AS last_end FROM statements "
                "GROUP BY account ORDER BY last_end DESC").fetchall()
        return [(r["account"], r["alias"] or "", date.fromisoformat(r["last_end"]) if r["last_end"] else None)
                for r in rows]

    def archive_version(self, account: Optional[str] = None) -> str:
        """Cheap identity of the archive contents - used as a cache key by the GUI."""
        q = "SELECT sha256 FROM statements" + (" WHERE account=?" if account else "") + " ORDER BY id"
        with self._conn() as c:
            rows = c.execute(q, (account,) if account else ()).fetchall()
        return "|".join(r["sha256"][:10] for r in rows)

    # ---- settings ---------------------------------------------------------------------------------------
    def get_settings(self) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key='settings'").fetchone()
        saved = json.loads(row["value"]) if row else {}
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
        return merged

    def save_settings(self, settings: dict) -> None:
        clean = {k: v for k, v in settings.items() if k in DEFAULT_SETTINGS}
        with self._conn() as c:
            c.execute("INSERT INTO kv(key, value) VALUES('settings', ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(clean),))

    def kv_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    # ---- market data cache -----------------------------------------------------------------------------
    def upsert_prices(self, symbol: str, rows: Sequence[tuple[date, float]], currency: Optional[str] = None,
                      name: Optional[str] = None) -> None:
        with self._conn() as c:
            c.executemany("INSERT INTO prices(symbol, day, close) VALUES(?,?,?) "
                          "ON CONFLICT(symbol, day) DO UPDATE SET close=excluded.close",
                          [(symbol, d.isoformat(), float(v)) for d, v in rows])
            c.execute(
                "INSERT INTO price_meta(symbol, currency, name, fetched_at, last_error) VALUES(?,?,?,?,NULL) "
                "ON CONFLICT(symbol) DO UPDATE SET currency=COALESCE(excluded.currency, price_meta.currency), "
                "name=COALESCE(excluded.name, price_meta.name), fetched_at=excluded.fetched_at, last_error=NULL",
                (symbol, currency, name, datetime.now().isoformat(timespec="seconds")))

    def set_price_error(self, symbol: str, message: str) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO price_meta(symbol, last_error, fetched_at) VALUES(?,?,?) "
                "ON CONFLICT(symbol) DO UPDATE SET last_error=excluded.last_error",
                (symbol, message[:500], datetime.now().isoformat(timespec="seconds")))

    def get_prices(self, symbol: str) -> list[tuple[date, float]]:
        with self._conn() as c:
            rows = c.execute("SELECT day, close FROM prices WHERE symbol=? ORDER BY day", (symbol,)).fetchall()
        return [(date.fromisoformat(r["day"]), float(r["close"])) for r in rows]

    def last_price_date(self, symbol: str) -> Optional[date]:
        with self._conn() as c:
            row = c.execute("SELECT MAX(day) AS d FROM prices WHERE symbol=?", (symbol,)).fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    def first_price_date(self, symbol: str) -> Optional[date]:
        with self._conn() as c:
            row = c.execute("SELECT MIN(day) AS d FROM prices WHERE symbol=?", (symbol,)).fetchone()
        return date.fromisoformat(row["d"]) if row and row["d"] else None

    def price_meta(self, symbol: str) -> dict:
        with self._conn() as c:
            row = c.execute("SELECT currency, name, fetched_at, last_error FROM price_meta WHERE symbol=?",
                            (symbol,)).fetchone()
        return dict(row) if row else {}

    # ---- manual price history (typed / pasted / uploaded by the user - NOT part of the downloaded cache) ----------
    def save_manual_prices(self, key: str, rows: Sequence[tuple[date, float]], source: str = "",
                           replace: bool = False) -> int:
        """Store ``(day, price)`` rows of ``key`` (``"TICKER|CCY"``).  A day that is already there is overwritten;
        with ``replace`` the whole series of ``key`` is replaced instead.  Returns the number of rows written."""
        stamp = datetime.now().isoformat(timespec="seconds")
        with self._conn() as c:
            if replace:
                c.execute("DELETE FROM manual_prices WHERE key=?", (key,))
            c.executemany(
                "INSERT INTO manual_prices(key, day, price, source, imported_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(key, day) DO UPDATE SET price=excluded.price, source=excluded.source, "
                "imported_at=excluded.imported_at",
                [(key, d.isoformat(), float(v), source, stamp) for d, v in rows])
        return len(rows)

    def get_manual_prices(self, key: str) -> list[tuple[date, float]]:
        with self._conn() as c:
            rows = c.execute("SELECT day, price FROM manual_prices WHERE key=? ORDER BY day", (key,)).fetchall()
        return [(date.fromisoformat(r["day"]), float(r["price"])) for r in rows]

    def manual_price_summary(self) -> dict[str, dict]:
        """``{key: {count, first, last, source, imported_at}}`` for every key that has manual prices."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT key, COUNT(*) AS n, MIN(day) AS first, MAX(day) AS last, MAX(source) AS source, "
                "MAX(imported_at) AS imported_at FROM manual_prices GROUP BY key ORDER BY key").fetchall()
        return {r["key"]: {"count": r["n"], "first": date.fromisoformat(r["first"]),
                           "last": date.fromisoformat(r["last"]), "source": r["source"] or "",
                           "imported_at": r["imported_at"] or ""} for r in rows}

    def delete_manual_prices(self, key: str, days: Optional[Sequence[date]] = None) -> int:
        """Remove the manual prices of ``key`` - all of them, or only those on ``days``.  Returns the row count."""
        with self._conn() as c:
            if days is None:
                cur = c.execute("DELETE FROM manual_prices WHERE key=?", (key,))
            else:
                cur = c.executemany("DELETE FROM manual_prices WHERE key=? AND day=?",
                                    [(key, d.isoformat()) for d in days])
            return cur.rowcount

    # ---- history of portfolio values (one row per valuation date, replaced when recomputed) -----------------
    def save_snapshot(self, account: str, as_of: date, values: dict) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO snapshots(account, as_of, taken_at, usd_total, base_total, usd_deposits, base_deposits, "
                "stocks_usd, cash_usd, base_currency) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account, as_of) DO UPDATE SET taken_at=excluded.taken_at, usd_total=excluded.usd_total, "
                "base_total=excluded.base_total, usd_deposits=excluded.usd_deposits, "
                "base_deposits=excluded.base_deposits, stocks_usd=excluded.stocks_usd, cash_usd=excluded.cash_usd, "
                "base_currency=excluded.base_currency",
                (account, as_of.isoformat(), datetime.now().isoformat(timespec="seconds"),
                 values.get("usd_total"), values.get("base_total"), values.get("usd_deposits"),
                 values.get("base_deposits"), values.get("stocks_usd"), values.get("cash_usd"),
                 values.get("base_currency")))

    def snapshots(self, account: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM snapshots WHERE account=? ORDER BY as_of", (account,)).fetchall()
        return [dict(r) for r in rows]

    def clear_market_cache(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM prices")
            c.execute("DELETE FROM price_meta")
