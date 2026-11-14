"""Market data service: refreshes the cache from Yahoo Finance and assembles :class:`MarketData`.

Data priority for every number (highest first):

1. a single manual price / rate typed into the ``Override`` column (settings / workbook) - it only decides the
   *current* value
2. the user's manual price history (``Date, Price`` pasted or uploaded in the Market data tab) - on the days it
   covers it wins over the downloaded value; the latest point of the merged series is the current price
3. Yahoo Finance data from the local cache (extended incrementally by *Refresh Prices*)
4. values derived from the statements themselves (period-end closes; works fully offline)

Steps 2-4 are merged per day into one :class:`~ibkr_tracker.market.data.PriceSeries` per stock
(``MarketData.price_series``) - the day-by-day history of the portfolio is valued with it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Iterable, Optional

from ..portfolio import Portfolio, is_closed, is_fund_type
from ..storage import Store
from .data import (SOURCE_LABEL, SRC_MANUAL, SRC_STATEMENT, SRC_YAHOO, BenchSeries, FxSeries, FxTable, MarketData,
                   PricePoint, PriceSeries, merge_series)
from .fallback import statement_fx, statement_price_points, statement_prices
from .manual import BENCHMARK_KEY
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


@dataclass
class ProfileReport:
    """Result of the sector lookup (*Look up sectors* in the dashboard) - what Yahoo Finance said about each stock."""
    found: dict[str, str] = field(default_factory=dict)         # "TICKER|CCY" -> sector
    no_sector: list[str] = field(default_factory=list)          # Yahoo knows the instrument but reports no sector
    errors: dict[str, str] = field(default_factory=dict)        # "TICKER|CCY" -> message
    cached: list[str] = field(default_factory=list)             # already known - not asked again
    skipped: list[str] = field(default_factory=list)            # funds, manual-only stocks, stocks you classified yourself
    offline: bool = False                                        # price source is 'statements only'
    unsupported: bool = False                                    # the price provider cannot look sectors up
    aborted: bool = False                                        # gave up early: Yahoo is evidently unreachable
    answered: int = 0
    messages: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.errors or self.offline or self.unsupported)

    def summary(self) -> str:
        if self.offline:
            return "Price source is 'statements only' - no sector lookup was made. Switch to Yahoo Finance in the sidebar."
        if self.unsupported:
            return "The price provider cannot look up sectors."
        parts = [f"{len(self.found)} sector(s) found"]
        if self.no_sector:
            parts.append(f"{len(self.no_sector)} without a sector at Yahoo")
        if self.errors:
            parts.append(f"{len(self.errors)} failed")
        if self.cached:
            parts.append(f"{len(self.cached)} already known")
        if self.skipped:
            parts.append(f"{len(self.skipped)} skipped (funds, manual-only or classified by you)")
        return "Sector lookup: " + ", ".join(parts) + "."


STALE_PRICE_DAYS = 10           # a held stock priced this many days before the valuation date gets a warning


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

    def auto_symbol(self, portfolio: Portfolio, ticker: str, ccy: str) -> str:
        """The symbol the automatic rule derives from the IBKR ticker and listing exchange (no alias applied)."""
        return self.symbol_for(portfolio, {}, ticker, ccy)

    @staticmethod
    def manual_only(settings: dict) -> set[str]:
        """``"TICKER|CCY"`` keys the user marked as 'never download - I supply the prices myself'."""
        return {str(k) for k in (settings.get("manual_only") or [])}

    @staticmethod
    def fx_symbol(ccy: str) -> str:
        return f"{ccy}=X"                     # Yahoo: units of CCY per 1 USD

    def required_symbols(self, portfolio: Portfolio, settings: dict) -> list[str]:
        skip = self.manual_only(settings)
        syms: list[str] = [self.symbol_for(portfolio, settings, t, c) for t, c in portfolio.positions_keys()
                           if f"{t}|{c}" not in skip]
        syms += [self.fx_symbol(c) for c in portfolio.currencies() if c != "USD"]    # incl. the EUR placeholder row
        if settings.get("benchmark_symbol"):
            syms.append(settings["benchmark_symbol"])
        return list(dict.fromkeys(syms))

    def sentinel_symbols(self, portfolio: Portfolio, settings: dict) -> set[str]:
        """FX pairs and the benchmark: symbols that certainly exist at Yahoo - when *they* cannot be downloaded the
        network is down, whereas a stock that fails alone merely has a naming problem."""
        out = {self.fx_symbol(c) for c in portfolio.currencies() if c != "USD"}
        if settings.get("benchmark_symbol"):
            out.add(settings["benchmark_symbol"])
        return out

    # ---- refresh (network) -----------------------------------------------------------------------------
    def refresh(self, portfolio: Portfolio, settings: dict,
                progress: Optional[Callable[[int, int, str], None]] = None,
                only: Optional[Iterable[str]] = None) -> RefreshReport:
        """Download the symbols that are needed (all of them, or just ``only`` - e.g. after a mapping change)."""
        report = RefreshReport(started=datetime.now())
        if settings.get("price_source", "yahoo") != "yahoo":
            report.offline = True
            report.finished = datetime.now()
            return report

        today = self._today()
        start_needed = (portfolio.first_activity() or today) - timedelta(days=14)
        symbols = self.required_symbols(portfolio, settings)
        if only is not None:
            wanted = set(only)
            symbols = [x for x in symbols if x in wanted]
        # FX pairs and the benchmark first: if those arrive, Yahoo is reachable and a failing stock is a naming
        # problem (fix it in the ticker mapping) - it must not make the refresh give up on everything else
        sentinels = self.sentinel_symbols(portfolio, settings)
        symbols.sort(key=lambda x: x not in sentinels)
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

    # ---- sectors (network, on request) -----------------------------------------------------------------
    def refresh_profiles(self, portfolio: Portfolio, settings: dict,
                         progress: Optional[Callable[[int, int, str], None]] = None,
                         force: bool = False) -> ProfileReport:
        """Ask the provider for the sector of every stock held now and cache the answers (``force``: ask again for the
        ones that are already known).  Funds (a fund has no sector), manual-only stocks and stocks the user has already
        given a sector are left out.  It is an explicit action - *Refresh Prices* never calls it."""
        report = ProfileReport()
        if settings.get("price_source", "yahoo") != "yahoo":
            report.offline = True
            return report
        lookup = getattr(self.provider, "profile", None)
        if not callable(lookup):
            report.unsupported = True
            return report
        held = portfolio.net_quantities()
        mine = settings.get("classification") or {}
        skip_download = self.manual_only(settings)
        cache = self.store.get_profiles()
        todo: list[tuple[str, str]] = []
        for ticker, ccy in portfolio.positions_keys():
            if is_closed(held.get((ticker, ccy), 0.0)):
                continue
            key = f"{ticker}|{ccy}"
            inst = portfolio.instruments.get(ticker)
            entry = mine.get(key) or mine.get(ticker) or {}
            if (key in skip_download or is_fund_type(inst.type if inst else "")
                    or (isinstance(entry, dict) and entry.get("sector"))):
                report.skipped.append(key)
                continue
            sym = self.symbol_for(portfolio, settings, ticker, ccy)
            known = cache.get(key)
            if not force and known and known["symbol"] == sym and known["sector"]:
                report.cached.append(key)
                continue
            todo.append((key, sym))
        for i, (key, sym) in enumerate(todo):
            if not report.answered and len(report.errors) >= self.GIVE_UP_AFTER:
                report.aborted = True
                report.messages.append(
                    f"Yahoo Finance could not be reached ({self.GIVE_UP_AFTER} lookups failed in a row) - the remaining "
                    f"{len(todo) - i} stock(s) were not asked. Check the internet connection and try again, or type the "
                    "sectors in yourself.")
                break
            if progress:
                progress(i, len(todo), sym)
            try:
                res = lookup(sym)
                report.answered += 1
                self.store.save_profile(key, sym, res.sector, res.industry, res.quote_type, res.name)
                if res.sector:
                    report.found[key] = res.sector
                else:
                    report.no_sector.append(key)
            except Exception as exc:                                  # noqa: BLE001 - never crash the GUI
                report.errors[key] = str(exc) if isinstance(exc, ProviderError) else repr(exc)
        if progress:
            progress(len(todo), len(todo), "")
        return report

    def cached_profiles(self, symbols: dict[str, str]) -> dict[str, dict]:
        """The cached profiles that belong to the symbols in use (``{"TICKER|CCY": symbol}``) - a profile that was looked
        up for another symbol (the mapping changed since) is not trusted."""
        return {k: v for k, v in self.store.get_profiles().items() if k in symbols and v["symbol"] == symbols[k]}

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
        # per stock one merged daily series: manual price history > Yahoo cache > statement period-end closes
        stmt_pts = statement_price_points(portfolio)
        skip_download = self.manual_only(settings)
        raw: dict[str, PricePoint] = {}
        series_by_key: dict[str, PriceSeries] = {}
        manual_keys: list[str] = []
        manual_current: list[str] = []
        symbols: dict[str, str] = {}
        yahoo_used = False
        held = portfolio.net_quantities()
        for ticker, ccy in portfolio.positions_keys():
            key = f"{ticker}|{ccy}"
            sym = self.symbol_for(portfolio, settings, ticker, ccy)
            symbols[key] = sym
            yahoo_rows: list[tuple[date, float]] = []
            if use_yahoo and key not in skip_download:
                rows = self.store.get_prices(sym)
                if rows:
                    meta_ccy = (self.store.price_meta(sym).get("currency") or "").upper()
                    if meta_ccy and meta_ccy != ccy.upper():
                        notes.append(f"{ticker}: Yahoo symbol {sym} is quoted in {meta_ccy} but IBKR trades it in "
                                     f"{ccy}; cached prices were ignored - set the correct Yahoo symbol.")
                    else:
                        yahoo_rows = rows
            manual_rows = self.store.get_manual_prices(key)
            if manual_rows:
                manual_keys.append(key)
            ser = merge_series([(SRC_MANUAL, manual_rows), (SRC_YAHOO, yahoo_rows),
                                (SRC_STATEMENT, stmt_pts.get(key, []))], sym)
            if not len(ser):
                # worth nothing without shares, and nothing to ask for when the user already typed a price
                if key not in price_over and not is_closed(held.get((ticker, ccy), 0.0)):
                    notes.append(f"No price available for {ticker} ({ccy}): refresh online, add a manual price "
                                 "history or enter a manual price.")
                continue
            series_by_key[key] = ser
            last_day, last_close, last_src = ser.dates[-1], ser.closes[-1], ser.sources[-1]
            if last_src == SRC_STATEMENT:
                point = stmt_px.get(key) or PricePoint(last_close, last_day, f"IBKR statement {last_day:%Y-%m-%d}",
                                                       ticker)
            else:
                point = PricePoint(last_close, last_day, SOURCE_LABEL[last_src], sym)
            yahoo_used |= last_src == SRC_YAHOO
            if last_src == SRC_MANUAL:
                manual_current.append(key)
            raw[key] = point

        # ---- FX -------------------------------------------------------------------------------------------
        active = set(portfolio.active_currencies())
        fx_ccys = sorted({c for c in portfolio.currencies() if c != "USD"})
        bench_sym = settings.get("benchmark_symbol") or ""
        b_yahoo = self.store.get_prices(bench_sym) if (use_yahoo and bench_sym) else []
        b_manual = self.store.get_manual_prices(BENCHMARK_KEY)       # quoted like the benchmark (USD unless Yahoo says
        b_ccy = (self.store.price_meta(bench_sym).get("currency") or "USD").upper() if b_yahoo else "USD"   # otherwise)
        b_ser = merge_series([(SRC_MANUAL, b_manual), (SRC_YAHOO, b_yahoo)], bench_sym)
        b_rows = list(zip(b_ser.dates, b_ser.closes))
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
                bench_source = ("Manual price history" if b_manual and not b_yahoo else
                                "Yahoo Finance + manual price history" if b_manual else "Yahoo Finance")
                bench = BenchSeries(bench_sym, b_ccy, [d for d, _ in b_rows], usd, [v for _, v in b_rows],
                                    bench_source)
        if not bench.dates and not bench_over:
            notes.append("No benchmark prices yet - the VUAA comparison needs one successful 'Refresh Prices' "
                         "(or a manual benchmark price / price history).")

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

        # a held stock whose newest price is far older than the valuation date is valued with stale data - say so
        # (typical for a manual price history that was typed in once and never extended)
        for key in pool:
            pt = raw[key]
            if (as_of - pt.day).days > STALE_PRICE_DAYS and key not in price_over:
                notes.append(f"{key.split('|')[0]}: the newest price is from {pt.day} ({pt.source}), {(as_of - pt.day).days} "
                             f"days before the valuation date {as_of} - add newer prices (Market data > Manual price "
                             "history) or enter a manual price.")

        if not use_yahoo:
            mode = "Statements only (offline mode)"
        elif yahoo_used:
            lr = self.store.kv_get("last_refresh") or "n/a"
            mode = f"Yahoo Finance (last refresh {lr}) with statement fallback"
        else:
            mode = "Statements only (no Yahoo data cached yet - press 'Refresh Prices')"
        if manual_current:
            mode += f" + manual price history for {len(manual_current)} stock(s)"

        return MarketData(
            prices=prices, raw_prices=raw, price_overrides=price_over, fx=fx, fx_latest_raw=latest_raw,
            fx_overrides=fx_over, benchmark=bench, benchmark_override=bench_over, as_of=as_of, mode=mode,
            yahoo_symbols=symbols, missing_fx=missing, notes=notes,
            last_refresh=self.store.kv_get("last_refresh") or "", price_series=series_by_key,
            manual_keys=manual_keys, profiles=self.cached_profiles(symbols))
