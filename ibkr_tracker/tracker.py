"""Application service layer - everything the GUI does, without any GUI code (so it can be tested headless).

Typical flow::

    t = Tracker()
    t.import_files([("U123_20260101_20261023.csv", data), ...])     # parse + archive (de-duplicated)
    out = t.refresh_prices(account)                                  # Yahoo -> cache -> workbook updated in place
    snap = t.snapshot(account)                                       # numbers for the dashboard
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Optional

from .calc import Metrics, compute_metrics
from .config import hide_closed_dust, output_dir
from .excel import (MissingMarketData, WorkbookLocked, WorkbookUpdate, find_soffice, open_current, refresh_workbook,
                    sync_overrides)
from .market import MarketData, MarketDataService, RefreshReport
from .market.providers import PriceProvider
from .models import StatementInfo
from .parsing import parse_statement
from .portfolio import CONSOLIDATED, Portfolio, consolidate, merge_statements
from .reconcile import CheckRow, reconcile
from .storage import Store


@dataclass
class ImportResult:
    filename: str
    ok: bool
    is_new: bool = False
    account: str = ""
    message: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class AccountSummary:
    """One line of the per-account breakdown of the consolidated view (valued with the consolidated market data)."""
    account: str
    alias: str
    base_currency: str
    period_end: Optional[date]
    stocks_usd: float
    cash_usd: float
    usd_total: float
    usd_deposits: float
    usd_return: Optional[float]
    usd_cagr: Optional[float]
    base_total: float                    # in the account's OWN base currency
    n_trades: int
    share: Optional[float] = None        # of the consolidated total value


@dataclass
class Snapshot:
    """Everything the dashboard shows for one account (or the consolidated view) at one moment."""
    portfolio: Portfolio
    settings: dict
    md: MarketData
    metrics: Optional[Metrics]           # None when the numbers cannot be computed (see ``problem``)
    checks: list[CheckRow]
    problem: str = ""
    accounts: list[AccountSummary] = field(default_factory=list)            # consolidated view: breakdown
    account_checks: dict[str, list[CheckRow]] = field(default_factory=dict)   # consolidated view: per-account checks


@dataclass
class RunOutcome:
    """Result of *Refresh Prices* or an explicit export."""
    report: Optional[RefreshReport] = None
    snapshot: Optional[Snapshot] = None
    update: Optional[WorkbookUpdate] = None
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class Tracker:
    def __init__(self, store: Optional[Store] = None, provider: Optional[PriceProvider] = None,
                 today: Optional[Callable[[], date]] = None):
        self.store = store or Store()
        self.service = MarketDataService(self.store, provider, today)

    # ---- settings ----------------------------------------------------------------------------------------
    def settings(self) -> dict:
        return self.store.get_settings()

    def update_settings(self, **changes) -> dict:
        s = self.store.get_settings()
        s.update(changes)
        self.store.save_settings(s)
        return s

    # ---- archive -----------------------------------------------------------------------------------------
    def import_statement(self, filename: str, data: bytes) -> ImportResult:
        try:
            ps = parse_statement(filename, data)
        except Exception as exc:                                    # noqa: BLE001 - show a friendly message
            return ImportResult(filename, False, message=f"{filename}: could not be read ({exc.__class__.__name__}: {exc}).")
        if not ps.account:
            return ImportResult(filename, False, message=f"{filename}: no account id found - is this an IBKR Activity "
                                                          f"Statement (CSV or HTML)?")
        _, is_new = self.store.add_statement(ps)
        msg = (f"{filename}: imported ({ps.period_start} to {ps.period_end}, {len(ps.trades)} stock trades)."
               if is_new else f"{filename}: already in the archive - nothing to do.")
        return ImportResult(filename, True, is_new, ps.account, msg, list(ps.warnings))

    def import_files(self, files: Iterable[tuple[str, bytes]]) -> list[ImportResult]:
        return [self.import_statement(name, data) for name, data in files]

    def statements(self, account: Optional[str] = None) -> list[StatementInfo]:
        return self.store.list_statements(None if account in (None, "", CONSOLIDATED) else account)

    def delete_statement(self, statement_id: int) -> None:
        self.store.delete_statement(statement_id)

    def accounts(self) -> list[tuple[str, str, Optional[date]]]:
        return self.store.accounts()

    def account_choices(self) -> list[str]:
        """What the GUI offers: the consolidated view first, then every account (empty without statements)."""
        accounts = [a for a, _, _ in self.accounts()]
        return [CONSOLIDATED, *accounts] if accounts else []

    def default_account(self) -> Optional[str]:
        accounts = [a for a, _, _ in self.accounts()]
        if not accounts:
            return None
        last = self.settings().get("last_account")
        return last if (last in accounts or last == CONSOLIDATED) else accounts[0]

    def portfolio(self, account: str, settings: Optional[dict] = None) -> Optional[Portfolio]:
        """Merged portfolio of one account - or, for ``CONSOLIDATED``, of all accounts as one unified portfolio."""
        if account == CONSOLIDATED:
            return self.consolidated_portfolio(settings)
        statements = self.store.load_statements(account)
        return merge_statements(statements, account) if statements else None

    def consolidated_portfolio(self, settings: Optional[dict] = None) -> Optional[Portfolio]:
        by_account: dict[str, list] = defaultdict(list)
        for s in self.store.load_statements():
            by_account[s.account].append(s)
        if not by_account:
            return None
        members = [merge_statements(sts, acc) for acc, sts in sorted(by_account.items())]
        base = (settings or self.settings()).get("consolidated_base") or None
        return consolidate(members, base)

    # ---- numbers -----------------------------------------------------------------------------------------
    def snapshot(self, account: str, settings: Optional[dict] = None,
                 portfolio: Optional[Portfolio] = None) -> Optional[Snapshot]:
        settings = settings or self.settings()
        p = portfolio or self.portfolio(account, settings)
        if p is None:
            return None
        return self._snapshot(p, settings)

    def _snapshot(self, p: Portfolio, settings: dict) -> Snapshot:
        md = self.service.build(p, settings)
        if md.missing_fx:
            return Snapshot(p, settings, md, None, [],
                            problem="No FX rate for " + ", ".join(md.missing_fx) +
                                    ". Press 'Refresh Prices' or enter a manual rate in the Market data tab.")
        days = float(settings.get("days_per_year") or 365)
        metrics = compute_metrics(p, md, days)
        snap = Snapshot(p, settings, md, metrics, reconcile(p, metrics))
        if p.is_consolidated:                    # every account valued with the same prices and rates
            for member in p.members:
                mm = compute_metrics(member, md, days)
                snap.account_checks[member.account] = reconcile(member, mm)
                snap.accounts.append(AccountSummary(
                    member.account, member.alias, member.base_currency, member.period_end, mm.stocks_usd, mm.cash_usd,
                    mm.usd_total, mm.usd_deposits, mm.usd_return, mm.usd_cagr, mm.base_total, len(member.trades),
                    share=(mm.usd_total / metrics.usd_total) if metrics.usd_total else None))
        return snap

    # ---- Excel -------------------------------------------------------------------------------------------
    def workbook_path(self, p: Portfolio, settings: Optional[dict] = None) -> Path:
        return output_dir(settings or self.settings()) / f"IBKR_Portfolio_{p.account}.xlsx"

    def workbook_status(self, p: Portfolio, settings: Optional[dict] = None) -> tuple[str, str, Path]:
        """``("missing"|"current"|"stale", detail, path)``."""
        settings = settings or self.settings()
        path = self.workbook_path(p, settings)
        if not path.exists():
            return "missing", "no workbook has been generated yet", path
        wb, reason = open_current(path, p, hide_closed_dust(settings))
        if wb is not None:
            return "current", "it matches the current statement archive", path
        return "stale", reason, path

    def _sync_overrides(self, p: Portfolio, settings: dict, out: RunOutcome) -> dict:
        """Adopt overrides typed into the workbook (three-way merge) and persist them."""
        merged, notes = sync_overrides(self.workbook_path(p, settings), settings, p)
        if notes:
            out.messages.extend(notes)
        if merged != settings:
            self.store.save_settings(merged)
        return merged

    def _write_workbook(self, p: Portfolio, snap: Snapshot, out: RunOutcome) -> None:
        path = self.workbook_path(p, snap.settings)
        try:
            out.update = refresh_workbook(path, p, snap.md, snap.settings)
        except MissingMarketData as exc:
            out.errors.append(str(exc))
        except WorkbookLocked as exc:
            out.errors.append(str(exc))
        except OSError as exc:
            out.errors.append(f"Could not write {path.name}: {exc}")
        except Exception as exc:                                     # noqa: BLE001
            out.errors.append(f"Excel export failed: {exc.__class__.__name__}: {exc}")
        else:
            out.messages.extend(out.update.messages)

    def export(self, account: str) -> RunOutcome:
        """Create / update / rebuild the workbook from the data already cached (no network)."""
        out = RunOutcome()
        settings = self.settings()
        p = self.portfolio(account, settings)
        if p is None:
            out.errors.append("No statements imported yet.")
            return out
        settings = self._sync_overrides(p, settings, out)
        out.snapshot = self._snapshot(p, settings)
        self._write_workbook(p, out.snapshot, out)
        return out

    # ---- refresh -----------------------------------------------------------------------------------------
    def refresh_prices(self, account: str,
                       progress: Optional[Callable[[int, int, str], None]] = None) -> RunOutcome:
        """Download new prices/FX (incrementally), then recalculate - and update the workbook in place when the
        *Export to Excel* option is on."""
        out = RunOutcome()
        settings = self.settings()
        p = self.portfolio(account, settings)
        if p is None:
            out.errors.append("No statements imported yet.")
            return out
        out.report = self.service.refresh(p, settings, progress)
        if out.report.offline:
            out.messages.append("Price source is 'statements only' - nothing was downloaded. "
                                "Switch to Yahoo Finance in the sidebar to refresh.")
        else:
            out.messages.extend(out.report.messages)
            if out.report.errors and not out.report.aborted:
                out.messages.append("Some symbols could not be downloaded; the cache / statement prices are used "
                                    "for them.")
        if settings.get("export_excel"):
            settings = self._sync_overrides(p, settings, out)
        out.snapshot = self._snapshot(p, settings)
        if out.snapshot.metrics is not None:
            self._record_history(p, out.snapshot)
        if settings.get("export_excel"):
            self._write_workbook(p, out.snapshot, out)
        return out

    def _record_history(self, p: Portfolio, snap: Snapshot) -> None:
        m = snap.metrics
        if m is None:
            return
        self.store.save_snapshot(p.account, snap.md.as_of, dict(
            usd_total=m.usd_total, base_total=m.base_total, usd_deposits=m.usd_deposits,
            base_deposits=m.base_deposits, stocks_usd=m.stocks_usd, cash_usd=m.cash_usd,
            base_currency=m.base_currency))

    def history(self, account: str) -> list[dict]:
        return self.store.snapshots(account)

    # ---- CSV export --------------------------------------------------------------------------------------
    def csv_files(self, snap: Snapshot, style: Optional[str] = None) -> dict[str, bytes]:
        """The tables of a snapshot (one account or the consolidated view) as CSV files: ``{file name: bytes}``.
        ``style``: ``"international"`` or ``"european"`` (default: the ``csv_format`` setting)."""
        from .exports import csv_files
        return csv_files(snap, style)

    def csv_zip(self, snap: Snapshot, style: Optional[str] = None) -> bytes:
        from .exports import csv_zip
        return csv_zip(snap, style)

    def write_csv(self, snap: Snapshot, style: Optional[str] = None) -> list[Path]:
        """Write the CSV files into ``<output folder>/IBKR_Portfolio_<account>_csv/``."""
        folder = output_dir(snap.settings) / f"IBKR_Portfolio_{snap.portfolio.account}_csv"
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for name, data in self.csv_files(snap, style).items():
            (folder / name).write_bytes(data)
            paths.append(folder / name)
        return paths

    # ---- misc --------------------------------------------------------------------------------------------
    @staticmethod
    def libreoffice_available() -> bool:
        return find_soffice() is not None
