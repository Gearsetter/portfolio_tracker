"""Market data service: refreshes the cache from Yahoo Finance and assembles :class:`MarketData`.

Data priority for every number (highest first):

1. manual overrides (settings / ``Override`` column of the workbook)
2. Yahoo Finance data from the local cache (extended incrementally by *Refresh Prices*)
3. values derived from the statements themselves (works fully offline)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from ..portfolio import Portfolio, is_closed
from ..storage import Store
from .data import BenchSeries, FxSeries, FxTable, MarketData, PricePoint
from .fallback import statement_fx, statement_prices
from .providers import PriceProvider, ProviderError, YahooProvider
from .symbols import yahoo_symbol


@dataclass
class RefreshReport:
    started: datetime
    finished: Optional[datetime] = None
    fetched: dict[str, int] = field(default_factory=dict)       # symbol -> rows written
    errors: dict[str, str] = field(default_factory=dict)        # symbol -> message
    offline: bool = False
    aborted: bool = False                                        # gave up early: Yahoo is evidently unreachable
    skipped: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.offline

    def summary(self) -> str:
        if self.offline:
            return "Offline mode (statement prices only) - nothing was downloaded."
        n = len(self.fetched)
        bad = len(self.errors)
        s = f"Updated {n} symbol(s)"
        if bad:
            s += f"; {bad} failed"
        if self.skipped:
            s += f"; {len(self.skipped)} skipped"
        return s + "."


class MarketDataService:
    GIVE_UP_AFTER = 3          # consecutive failures (with no success at all) before a refresh is abandoned

    def __init__(self, store: Store, provider: Optional[PriceProvider] = None,
                 today: Optional[Callable[[], date]] = None):
        self.store = store
        self.provider: PriceProvider = provider or YahooProvider()
        self._today = today or date.today

    # ---- symbols -----------------------------------------------------------------------------------------
    def symbol_for(self, portfolio: Portfolio, settings: dict, ticker: str, ccy: str) -> str:
        inst = portfolio.instruments.get(ticker)
        return yahoo_symbol(ticker, inst.exchange if inst else "", ccy, settings.get("yahoo_symbols") or {})

    @staticmethod
    def fx_symbol(ccy: str) -> str:
        return f"{ccy}=X"                     # Yahoo: units of CCY per 1 USD

    def required_symbols(self, portfolio: Portfolio, settings: dict) -> list[str]:
        syms: list[str] = [self.symbol_for(portfolio, settings, t, c) for t, c in portfolio.positions_keys()]
        syms += [self.fx_symbol(c) for c in portfolio.currencies() if c != "USD"]    # incl. the EUR placeholder row
        if settings.get("benchmark_symbol"):
            syms.append(settings["benchmark_symbol"])
        return list(dict.fromkeys(syms))

    # ---- refresh (network) -----------------------------------------------------------------------------
    def refresh(self, portfolio: Portfolio, settings: dict,
                progress: Optional[Callable[[int, int, str], None]] = None) -> RefreshReport:
        report = RefreshReport(started=datetime.now())
        if settings.get("price_source", "yahoo") != "yahoo":
            report.offline = True
            report.finished = datetime.now()
            return report

        today = self._today()
        start_needed = (portfolio.first_activity() or today) - timedelta(days=14)
        symbols = self.required_symbols(portfolio, settings)
        include_intraday = bool(settings.get("include_intraday", False))
        for i, sym in enumerate(symbols):
            if not report.fetched and len(report.errors) >= self.GIVE_UP_AFTER:
                # nothing worked N times in a row -> the network / Yahoo is down; do not make the user wait
                report.aborted = True
                report.skipped = symbols[i:]
                report.messages.append(
                    f"Yahoo Finance could not be reached ({self.GIVE_UP_AFTER} symbols failed in a row) - the "
                    f"remaining {len(report.skipped)} symbol(s) were skipped. Check the internet connection / proxy "
                    "and press Refresh Prices again; meanwhile cached and statement prices are used.")
                break
            if progress:
                progress(i, len(symbols), sym)
            try:
                last = self.store.last_price_date(sym)
                first = self.store.first_price_date(sym)
                if last is None or first is None or first > start_needed + timedelta(days=10):
                    fetch_start = start_needed                       # nothing cached / cache starts too late
                else:
                    fetch_start = last - timedelta(days=7)           # incremental, re-reading a week of overlap
                res = self.provider.history(sym, fetch_start, today, include_intraday=include_intraday)
                self.store.upsert_prices(sym, res.rows, res.currency, res.name)
                report.fetched[sym] = len(res.rows)
            except ProviderError as exc:
                self.store.set_price_error(sym, str(exc))
                report.errors[sym] = str(exc)
            except Exception as exc:                                  # noqa: BLE001 - never crash the GUI
                self.store.set_price_error(sym, repr(exc))
                report.errors[sym] = repr(exc)
        if progress:
            progress(len(symbols), len(symbols), "")
        if report.fetched:
            self.store.kv_set("last_refresh", datetime.now().isoformat(timespec="seconds"))
        report.finished = datetime.now()
        return report

    # ---- assemble (no network) -------------------------------------------------------------------------
    def build(self, portfolio: Portfolio, settings: dict) -> MarketData:
        use_yahoo = settings.get("price_source", "yahoo") == "yahoo"
        notes: list[str] = []
        stmt_px = statement_prices(portfolio)
        stmt_fx = statement_fx(portfolio)
        keys = [f"{t}|{c}" for t, c in portfolio.positions_keys()]

        price_over = {k: float(v) for k, v in (settings.get("price_overrides") or {}).items()
                      if v not in (None, "") and k in keys}
        fx_over = {c: float(v) for c, v in (settings.get("fx_overrides") or {}).items() if v not in (None, "")}
        bench_over = settings.get("benchmark_override")
        bench_over = float(bench_over) if bench_over not in (None, "", 0) else None

        # ---- stock prices ---------------------------------------------------------------------------------
        raw: dict[str, PricePoint] = {}
        symbols: dict[str, str] = {}
        yahoo_used = False
        held = portfolio.net_quantities()
        for ticker, ccy in portfolio.positions_keys():
            key = f"{ticker}|{ccy}"
            sym = self.symbol_for(portfolio, settings, ticker, ccy)
            symbols[key] = sym
            cached: Optional[PricePoint] = None
            if use_yahoo:
                rows = self.store.get_prices(sym)
                if rows:
                    meta_ccy = (self.store.price_meta(sym).get("currency") or "").upper()
                    if meta_ccy and meta_ccy != ccy.upper():
                        notes.append(f"{ticker}: Yahoo symbol {sym} is quoted in {meta_ccy} but IBKR trades it in "
                                     f"{ccy}; cached prices were ignored - set the correct Yahoo symbol.")
                    else:
                        cached = PricePoint(rows[-1][1], rows[-1][0], "Yahoo Finance", sym)
            stmt = stmt_px.get(key)
            chosen = cached if cached and (stmt is None or cached.day >= stmt.day) else stmt
            if chosen is None:
                # worth nothing without shares, and nothing to ask for when the user already typed a price
                if key not in price_over and not is_closed(held.get((ticker, ccy), 0.0)):
                    notes.append(f"No price available for {ticker} ({ccy}): refresh online or enter a manual price.")
                continue
            yahoo_used |= chosen is cached
            raw[key] = chosen

        # ---- FX -------------------------------------------------------------------------------------------
        active = set(portfolio.active_currencies())
        fx_ccys = sorted({c for c in portfolio.currencies() if c != "USD"})
        bench_sym = settings.get("benchmark_symbol") or ""
        b_rows = self.store.get_prices(bench_sym) if (use_yahoo and bench_sym) else []
        b_ccy = (self.store.price_meta(bench_sym).get("currency") or "USD").upper() if b_rows else "USD"
        if b_rows and b_ccy != "USD" and b_ccy not in fx_ccys:
            fx_ccys.append(b_ccy)

        series: dict[str, FxSeries] = {}
        latest_raw: dict[str, PricePoint] = {}
        missing: list[str] = []
        first_day = portfolio.first_activity() or self._today()
        for ccy in fx_ccys:
            y_rows = self.store.get_prices(self.fx_symbol(ccy)) if use_yahoo else []
            s_ser = stmt_fx.get(ccy)
            if y_rows:
                ser = FxSeries([d for d, _ in y_rows], [v for _, v in y_rows], "Yahoo Finance")
                if s_ser and s_ser.dates[-1] > ser.dates[-1]:         # a newer statement closing rate
                    extra = [(d, r) for d, r in zip(s_ser.dates, s_ser.rates) if d > ser.dates[-1]]
                    ser = FxSeries(ser.dates + [d for d, _ in extra], ser.rates + [r for _, r in extra],
                                   "Yahoo Finance + IBKR statement")
                yahoo_used = True
            elif s_ser:
                ser = s_ser
            elif ccy in fx_over:
                ser = FxSeries([first_day], [fx_over[ccy]], "Manual override")
            else:
                if ccy in active:                       # placeholder rows (e.g. EUR without activity) may stay empty
                    missing.append(ccy)
                continue
            series[ccy] = ser
            rate, day = ser.last
            latest_raw[ccy] = PricePoint(rate, day, ser.source, self.fx_symbol(ccy))
        fx = FxTable(series)

        # ---- benchmark -----------------------------------------------------------------------------------
        bench = BenchSeries(symbol=bench_sym, currency="USD")
        if b_rows:
            if b_ccy == "USD":
                usd = [v for _, v in b_rows]
            elif b_ccy in series:
                usd = [v / series[b_ccy].asof(d) for d, v in b_rows]
            else:
                usd = []
                notes.append(f"Benchmark {bench_sym} is quoted in {b_ccy} but no {b_ccy} FX rate is available.")
            if usd:
                bench = BenchSeries(bench_sym, b_ccy, [d for d, _ in b_rows], usd, [v for _, v in b_rows],
                                    "Yahoo Finance")
        if not bench.dates and not bench_over:
            notes.append("No benchmark prices yet - the VUAA comparison needs one successful 'Refresh Prices' "
                         "(or a manual benchmark price).")

        # ---- as-of date -----------------------------------------------------------------------------------
        held = [f"{t}|{c}" for (t, c), q in portfolio.net_quantities().items() if abs(q) > 1e-9]
        pool = [k for k in (held or keys) if k in raw]
        days = [raw[k].day for k in pool]
        as_of = max(days) if days else (portfolio.period_end or self._today())

        prices = dict(raw)
        for key, value in price_over.items():
            base_pt = raw.get(key)
            prices[key] = PricePoint(value, base_pt.day if base_pt else as_of, "Manual override",
                                     base_pt.symbol if base_pt else symbols.get(key, ""))
        if missing:
            notes.append("No FX rates for: " + ", ".join(missing) + " (refresh online or enter manual rates).")

        if not use_yahoo:
            mode = "Statements only (offline mode)"
        elif yahoo_used:
            lr = self.store.kv_get("last_refresh") or "n/a"
            mode = f"Yahoo Finance (last refresh {lr}) with statement fallback"
        else:
            mode = "Statements only (no Yahoo data cached yet - press 'Refresh Prices')"

        return MarketData(
            prices=prices, raw_prices=raw, price_overrides=price_over, fx=fx, fx_latest_raw=latest_raw,
            fx_overrides=fx_over, benchmark=bench, benchmark_override=bench_over, as_of=as_of, mode=mode,
            yahoo_symbols=symbols, missing_fx=missing, notes=notes,
            last_refresh=self.store.kv_get("last_refresh") or "")
