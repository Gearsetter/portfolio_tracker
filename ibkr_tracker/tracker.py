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

from . import allocation as alloc
from . import analytics
from .calc import Metrics, compute_metrics
from .config import hide_closed_dust, output_dir
from .excel import (MissingMarketData, WorkbookLocked, WorkbookUpdate, find_soffice, open_current, refresh_workbook,
                    sync_overrides)
from .history import History, build_history
from .market import MarketData, MarketDataService, ProfileReport, RefreshReport
from .market import manual as manual_prices
from .market import mapping as ticker_mapping
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
    _history: Optional[History] = field(default=None, repr=False, compare=False)
    _holdings: Optional[list] = field(default=None, repr=False, compare=False)
    _risk: dict = field(default_factory=dict, repr=False, compare=False)

    def history(self) -> Optional[History]:
        """The portfolio reconstructed day by day from its first transaction to the valuation date (see
        :mod:`ibkr_tracker.history`); computed once per snapshot, ``None`` when the numbers cannot be computed."""
        if self.metrics is None:
            return None
        if self._history is None:
            self._history = build_history(self.portfolio, self.md, self.metrics,
                                          float(self.settings.get("days_per_year") or 365))
        return self._history

    def holdings(self) -> list[alloc.Holding]:
        """The stocks held now and the cash balances, each with its asset class, stock type and industry sector (the
        sectors come from the user's own entries first, then from the cached Yahoo profiles - see
        :mod:`ibkr_tracker.allocation`).  Honours the view filter; computed once per snapshot."""
        if self.metrics is None:
            return []
        if self._holdings is None:
            self._holdings = alloc.classify(self.portfolio, self.metrics, self.settings, self.md.profiles,
                                            hide_closed_dust(self.settings))
        return self._holdings

    def allocation(self, by: str) -> alloc.Allocation:
        """Weights of :meth:`holdings` grouped by ``by`` (a key of :data:`ibkr_tracker.allocation.GROUPINGS`)."""
        return alloc.allocate(self.holdings(), by)

    def risk(self, unit: str = "usd", since: Optional[date] = None,
             risk_free: Optional[float] = None) -> analytics.RiskReport:
        """Risk and risk-adjusted return from the day-by-day history, in USD or the base currency, from ``since`` (``None``
        = the whole lifespan) to the valuation date.  ``risk_free`` is the annual rate as a fraction; by default the one
        entered for that currency (``settings["risk_free"]``, 0 when none)."""
        h = self.history()
        u = analytics.unit_of(h, unit) if h is not None else "usd"
        if risk_free is None:
            ccy = h.base_currency if (h is not None and u == "base") else "USD"
            risk_free = analytics.risk_free_rate(self.settings, ccy)
        key = (u, since, risk_free)
        if key not in self._risk:
            self._risk[key] = analytics.risk_report(h, self.md, u, risk_free, since,
                                                    float(self.settings.get("days_per_year") or 365))
        return self._risk[key]


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


@dataclass
class MappingOutcome:
    """Result of saving / importing the ticker mapping."""
    changed: list[str] = field(default_factory=list)          # "TICKER|CCY" keys whose symbol or mode changed
    errors: list[str] = field(default_factory=list)           # edits that were rejected
    report: Optional[RefreshReport] = None                    # the download of the changed symbols (if requested)
    messages: list[str] = field(default_factory=list)


@dataclass
class ProfileOutcome:
    """Result of *Look up sectors*."""
    report: Optional[ProfileReport] = None
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
        """The values recorded at each *Refresh Prices* (legacy).  The dashboard charts the day-by-day
        reconstruction instead - :meth:`Snapshot.history` - which does not depend on when prices were refreshed."""
        return self.store.snapshots(account)

    # ---- ticker mapping (IBKR ticker -> Yahoo Finance symbol) ---------------------------------------------------
    def mapping_rows(self, p: Portfolio, settings: Optional[dict] = None) -> list[ticker_mapping.MappingRow]:
        """One row per stock of ``p``: automatic symbol, the user's alias, manual-only flag and download status."""
        return ticker_mapping.mapping_rows(self.store, self.service, p, settings or self.settings())

    def save_mapping(self, p: Portfolio, edits: dict[str, tuple[str, bool]], download: bool = True,
                     progress: Optional[Callable[[int, int, str], None]] = None) -> MappingOutcome:
        """Persist the alias / manual-only edits (``{"TICKER|CCY": (alias, manual_only)}`` of the shown rows) and - with
        ``download`` - fetch the prices of the symbols that changed, so the new mapping takes effect at once."""
        settings = self.settings()
        change = ticker_mapping.apply_edits(settings, self.mapping_rows(p, settings), edits)
        return self._store_mapping(p, settings, change, download, progress)

    def import_mapping(self, p: Portfolio, text: str, download: bool = True,
                       progress: Optional[Callable[[int, int, str], None]] = None) -> MappingOutcome:
        """Merge a mapping file (``Ticker,Currency,Symbol,Manual only`` CSV) into the saved mapping."""
        settings = self.settings()
        entries, problems = ticker_mapping.parse_mapping_csv(text)
        change = ticker_mapping.merge_import(settings, entries)
        change.errors = problems
        return self._store_mapping(p, settings, change, download, progress)

    def _store_mapping(self, p: Portfolio, settings: dict, change: ticker_mapping.MappingChange, download: bool,
                       progress: Optional[Callable[[int, int, str], None]]) -> MappingOutcome:
        out = MappingOutcome(changed=list(change.changed), errors=list(change.errors))
        if change.changed:
            settings = self.update_settings(yahoo_symbols=change.aliases, manual_only=change.manual_only)
            out.messages.append(f"Mapping saved ({len(change.changed)} stock(s) changed).")
        if download and change.changed and settings.get("price_source", "yahoo") == "yahoo":
            skip = self.service.manual_only(settings)
            wanted = {self.service.symbol_for(p, settings, t, c) for t, c in p.positions_keys()
                      if f"{t}|{c}" not in skip and (f"{t}|{c}" in change.changed or t in change.changed)}
            if wanted:
                out.report = self.service.refresh(p, settings, progress, only=wanted)
                out.messages.append(out.report.summary())
        return out

    def mapping_csv(self) -> str:
        """The saved mapping (all accounts) as a downloadable file."""
        return ticker_mapping.mapping_to_csv(self.settings())

    def check_symbol(self, symbol: str, currency: str = "") -> ticker_mapping.SymbolTest:
        """Ask the price provider about ``symbol`` (the *Test* button of the mapping table)."""
        return ticker_mapping.check_symbol(self.service.provider, symbol, currency, self.service._today())

    # ---- manual price history ---------------------------------------------------------------------------------
    @staticmethod
    def parse_manual_prices(text: str, **options) -> manual_prices.ParsedPrices:
        """Read pasted ``Date, Price`` text (nothing is saved).  Raises :class:`manual_prices.PriceParseError`."""
        return manual_prices.parse_prices(text, **options)

    @staticmethod
    def parse_manual_prices_file(data: bytes, **options) -> manual_prices.ParsedPrices:
        """Read an uploaded CSV / text file (nothing is saved)."""
        return manual_prices.parse_price_file(data, **options)

    def save_manual_prices(self, key: str, rows, source: str = "pasted text", replace: bool = False) -> int:
        """Store the manual prices of ``key`` (``"TICKER|CCY"``, or ``manual_prices.BENCHMARK_KEY``).  Rows of days that
        already have a manual price overwrite it; with ``replace`` the stock's whole manual history is replaced."""
        return self.store.save_manual_prices(key, rows, source, replace)

    def manual_prices(self, key: str) -> list[tuple[date, float]]:
        return self.store.get_manual_prices(key)

    def manual_price_summary(self) -> dict[str, dict]:
        return self.store.manual_price_summary()

    def delete_manual_prices(self, key: str, days=None) -> int:
        return self.store.delete_manual_prices(key, days)

    # ---- sectors and classification (allocation) ------------------------------------------------------------------
    def lookup_profiles(self, account: str, progress: Optional[Callable[[int, int, str], None]] = None,
                        force: bool = False) -> ProfileOutcome:
        """Ask Yahoo Finance for the sector of every stock held (an explicit action - *Refresh Prices* does not do it).
        The answers are cached in the archive; stocks that already have one are not asked again unless ``force``."""
        out = ProfileOutcome()
        settings = self.settings()
        p = self.portfolio(account, settings)
        if p is None:
            out.errors.append("No statements imported yet.")
            return out
        out.report = self.service.refresh_profiles(p, settings, progress, force)
        out.messages.append(out.report.summary())
        out.messages.extend(out.report.messages)
        for key, err in out.report.errors.items():
            out.errors.append(f"{key.split('|')[0]}: {err}")
        return out

    def save_classification(self, holdings: list[alloc.Holding],
                            edits: dict[str, tuple[str, str]]) -> alloc.ClassificationChange:
        """Persist the asset classes / sectors typed into the classification table (``{"TICKER|CCY": (asset class,
        sector)}`` of the shown rows).  An entry that equals the automatic value is not stored."""
        settings = self.settings()
        change = alloc.apply_classification_edits(settings, holdings, edits)
        if change.changed:
            self.update_settings(classification=change.classification)
        return change

    def set_risk_free(self, currency: str, percent: float) -> dict:
        """Remember the annual risk-free rate (in percent) used for ``currency`` in the Sharpe / Sortino / alpha figures."""
        rates = dict(self.settings().get("risk_free") or {})
        rates[currency] = float(percent)
        return self.update_settings(risk_free=rates)

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
