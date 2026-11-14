"""IBKR Portfolio Tracker - Streamlit GUI.

Start with::

    streamlit run app.py          (or:  python app.py)

Upload one or more IBKR Activity Statements (.csv / .htm) in the sidebar.  They are archived locally
(SQLite, ``./data`` or ``$IBKR_TRACKER_HOME``), merged chronologically without double counting, valued with
Yahoo Finance prices (falling back to the prices printed in the statements) and - optionally - exported to a
formula-driven Excel workbook that *Refresh Prices* keeps up to date in place.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ibkr_tracker import __version__                                   # noqa: E402
from ibkr_tracker import tables as T                                   # noqa: E402
from ibkr_tracker.market.symbols import yahoo_symbol                   # noqa: E402
from ibkr_tracker.reconcile import overall                             # noqa: E402
from ibkr_tracker.tracker import RunOutcome, Snapshot, Tracker         # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_VERSION = tuple(int(x) for x in st.__version__.split(".")[:2] if x.isdigit())
_NEW_WIDTH_API = _VERSION >= (1, 50)


# ======================================================================================================
# small helpers
# ======================================================================================================
@st.cache_resource(show_spinner=False)
def get_tracker() -> Tracker:
    return Tracker()


def stretch() -> dict:
    """``width='stretch'`` on new Streamlit versions, ``use_container_width=True`` on older ones."""
    return {"width": "stretch"} if _NEW_WIDTH_API else {"use_container_width": True}


def show_df(df: pd.DataFrame, column_config: Optional[dict] = None, height: Optional[int] = None) -> None:
    kw = dict(hide_index=True, column_config=column_config, **stretch())
    if height:
        kw["height"] = height
    st.dataframe(df, **kw)


def fmt_money(v: Optional[float], ccy: str = "", digits: int = 2) -> str:
    if v is None or v != v:
        return "n/a"
    return f"{v:,.{digits}f}" + (f" {ccy}" if ccy else "")


def fmt_pct(v: Optional[float], signed: bool = True) -> str:
    if v is None or v != v:
        return "n/a"
    return f"{v * 100:+.2f}%" if signed else f"{v * 100:.2f}%"


def fmt_mult(v: Optional[float]) -> str:
    return "n/a" if v is None or v != v else f"{v:.4f}x"


def kpi(col, label: str, value: str, delta: Optional[str] = None, help: Optional[str] = None) -> None:
    with col:
        with st.container(border=True, height=122):
            st.metric(label, value, delta=delta, help=help)


def flash(level: str, text: str) -> None:
    st.session_state.setdefault("flash", []).append((level, text))


def show_flash() -> None:
    """Show the messages queued by the last action - one compact box per severity."""
    items = st.session_state.pop("flash", [])
    for level, box in (("error", st.error), ("warning", st.warning), ("success", st.success), ("info", st.info)):
        texts = list(dict.fromkeys(t for lv, t in items if lv == level))
        if texts:
            box(texts[0] if len(texts) == 1 else "\n".join(f"- {t}" for t in texts))


def short_error(message: str, limit: int = 180) -> str:
    """One readable line out of a long provider exception text."""
    text = re.sub(r"https?://\S+", "", message or "").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip(" ;.")
    return text if len(text) <= limit else text[: limit - 1] + "..."


def post_outcome(out: RunOutcome, what: str) -> None:
    for m in out.messages:
        flash("info", m)
    for e in out.errors:
        flash("error", e)
    if out.update is not None:
        verb = {"updated": "updated in place", "rebuilt": "rebuilt (the statement archive changed)",
                "created": "created"}[out.update.action]
        flash("success", f"{what}: Excel workbook {verb}: {out.update.path}")
        if out.update.backup:
            flash("info", f"The previous workbook was kept as {out.update.backup.name}.")


# ======================================================================================================
# sidebar
# ======================================================================================================
def sidebar(tracker: Tracker) -> tuple[Optional[str], dict, bool]:
    """Draws the sidebar; returns ``(account, settings, refresh_clicked)``."""
    settings = tracker.settings()
    with st.sidebar:
        st.markdown("## 📈 IBKR Portfolio Tracker")
        st.caption(f"v{__version__}")

        # ---- import ---------------------------------------------------------------------------------
        nonce = st.session_state.setdefault("uploader_nonce", 0)
        files = st.file_uploader(
            "Import Activity Statements", type=["csv", "htm", "html"], accept_multiple_files=True,
            key=f"uploader_{nonce}",
            help="IBKR Activity Statements (Reports > Statements > Activity) as CSV and/or HTML. Several files "
                 "and both formats can be mixed - duplicates are detected and counted once.")
        if files:
            results = tracker.import_files([(f.name, f.getvalue()) for f in files])
            for r in results:
                flash("success" if (r.ok and r.is_new) else ("info" if r.ok else "error"), r.message)
                for w in r.warnings:
                    flash("warning", f"{r.filename}: {w}")
            touched = sorted({r.account for r in results if r.ok})
            if touched:
                st.session_state["account"] = touched[0]
                tracker.update_settings(last_account=touched[0])
                if any(r.is_new for r in results) and settings.get("export_excel"):
                    with st.spinner("Updating the Excel workbook ..."):
                        post_outcome(tracker.export(touched[0]), "Import")
            st.session_state["uploader_nonce"] = nonce + 1
            st.rerun()

        # ---- account --------------------------------------------------------------------------------
        accounts = tracker.accounts()
        account: Optional[str] = None
        if accounts:
            labels = {a: a + (f"  ·  {alias}" if alias else "") for a, alias, _ in accounts}
            options = list(labels)
            if st.session_state.get("account") not in options:
                st.session_state["account"] = tracker.default_account()
            account = st.selectbox("Account", options, format_func=labels.get, key="account")
            if account != settings.get("last_account"):
                settings = tracker.update_settings(last_account=account)
        else:
            st.info("No statements yet - upload one or more Activity Statements above.")

        # ---- refresh --------------------------------------------------------------------------------
        refresh = st.button("🔄  Refresh Prices", type="primary", disabled=account is None, key="refresh",
                            help="Downloads new prices and FX rates (only what is missing), recalculates everything "
                                 "and - if Excel export is on - updates the existing workbook in place.",
                            **stretch())

        # ---- settings -------------------------------------------------------------------------------
        st.divider()
        export = st.toggle("Export results as Excel file", value=bool(settings.get("export_excel")), key="w_export",
                           help="When on, importing statements and pressing Refresh Prices also writes/updates "
                                "the formula-driven .xlsx workbook.")
        with st.expander("Settings", expanded=False):
            source = st.selectbox(
                "Price source", ["yahoo", "statement"], key="w_source",
                index=0 if settings.get("price_source", "yahoo") == "yahoo" else 1,
                format_func={"yahoo": "Yahoo Finance (online)", "statement": "Statements only (offline)"}.get,
                help="Offline mode never touches the network and values holdings with the closing prices printed "
                     "in the statements.")
            bench = st.text_input("Benchmark (Yahoo symbol)", value=settings.get("benchmark_symbol", "VUAA.L"),
                                  key="w_bench", help="S&P 500 ETF used for the 'what if I had bought the index' "
                                                      "simulation. Must be quoted in USD (or a currency with an "
                                                      "FX rate), e.g. VUAA.L, VUAA.MI, SPY.")
            days = st.number_input("Days per year (CAGR)", min_value=360.0, max_value=366.0, step=0.25,
                                   value=float(settings.get("days_per_year", 365)), key="w_days")
            out_dir = st.text_input("Output folder (empty = <data folder>/output)", value=settings.get("output_dir", ""),
                                    key="w_outdir")
            st.caption(f"Data folder: `{tracker.store.path.parent}`  (set IBKR_TRACKER_HOME to change)")
            lo_ok = tracker.libreoffice_available()
            recalc = st.checkbox("Embed calculated values (needs LibreOffice)", value=bool(settings.get("recalc_with_libreoffice")) and lo_ok,
                                 disabled=not lo_ok, key="w_recalc",
                                 help="Excel recalculates the workbook anyway. Embedding values only makes the "
                                      "file readable in previewers that do not calculate."
                                      + ("" if lo_ok else " LibreOffice was not found on this computer."))
        new = dict(export_excel=bool(export), price_source=source, benchmark_symbol=bench.strip() or "VUAA.L",
                   days_per_year=float(days), output_dir=out_dir.strip(),
                   recalc_with_libreoffice=bool(recalc) if lo_ok else bool(settings.get("recalc_with_libreoffice")))
        if any(settings.get(k) != v for k, v in new.items()):
            was_on = bool(settings.get("export_excel"))
            settings = tracker.update_settings(**new)
            if new["export_excel"] and not was_on and account:         # just switched on: write the file right away
                with st.spinner("Writing the Excel workbook ..."):
                    post_outcome(tracker.export(account), "Export")
    return account, settings, bool(refresh)


# ======================================================================================================
# dashboard
# ======================================================================================================
def dashboard(snap: Snapshot) -> None:
    m, md, p, s = snap.metrics, snap.md, snap.portfolio, snap.settings
    base = m.base_currency
    bench_label = (s.get("benchmark_symbol") or "benchmark").split(".")[0]

    c = st.columns(4)
    kpi(c[0], "Total value (USD)", fmt_money(m.usd_total, "USD"), help="Σ USD Value of positions + cash (Sections 6-8).")
    kpi(c[1], "Net deposits (USD)", fmt_money(m.usd_deposits, "USD"),
        help="Σ USD Amount of deposits, withdrawals and monthly fees (Section 3), converted at each day's rate.")
    kpi(c[2], "Return (USD)", fmt_mult(m.usd_return), fmt_pct(None if m.usd_return is None else m.usd_return - 1),
        help="Total value / net deposits.")
    kpi(c[3], "CAGR (USD)", fmt_pct(m.usd_cagr, signed=False),
        help="Time-weighted: Return^(1/years) - 1 with years = deposit-weighted average time since the deposits "
             f"({m.years:.3f} y)." if m.years else "n/a")
    c = st.columns(4)
    kpi(c[0], f"Total value ({base})", fmt_money(m.base_total, base, 0), help="Total USD value x latest USD rate.")
    kpi(c[1], f"Net deposits ({base})", fmt_money(m.base_deposits, base, 0))
    kpi(c[2], f"Return ({base})", fmt_mult(m.base_return), fmt_pct(None if m.base_return is None else m.base_return - 1),
        help="Includes the currency effect: deposits are valued at the rate of their day.")
    kpi(c[3], f"CAGR ({base})", fmt_pct(m.base_cagr, signed=False))

    b = m.benchmark
    c = st.columns(4)
    kpi(c[0], "Stocks (USD)", fmt_money(m.stocks_usd, "USD"))
    kpi(c[1], "Cash (USD)", fmt_money(m.cash_usd, "USD"))
    if b.available:
        kpi(c[2], f"{bench_label} instead (USD)", fmt_money(b.value_usd, "USD"),
            fmt_pct(None if b.ret_vuaa is None else b.ret_vuaa - 1),
            help=f"Every stock purchase's USD total invested in {s.get('benchmark_symbol')} on the same day "
                 f"({b.units:,.4f} units) and valued at {b.price:,.2f} USD.")
        kpi(c[3], f"Stocks minus {bench_label} (USD)", fmt_money(b.excess_usd, "USD"),
            help="Value of the stock positions minus the simulated benchmark position.")
    else:
        kpi(c[2], f"{bench_label} instead (USD)", "n/a")
        kpi(c[3], f"Stocks minus {bench_label} (USD)", "n/a", help="Needs benchmark prices: press Refresh Prices "
                                                                    "(or enter a manual benchmark price).")

    st.markdown("#### Positions")
    pos = T.positions_table(m)
    pos["Gain"] = (pd.to_numeric(pos["Return"], errors="coerce") - 1) * 100
    pos["USD Gain"] = (pd.to_numeric(pos["USD Return"], errors="coerce") - 1) * 100
    show_df(pos[["Ticker", "Name", "Currency", "Quantity", "Current Price", "Value", "Return", "Gain", "USD Value",
                 "USD Return", "USD Gain"]], column_config={
        "Quantity": st.column_config.NumberColumn(format="%.4f"),
        "Current Price": st.column_config.NumberColumn(format="%.2f"),
        "Value": st.column_config.NumberColumn(format="%.2f"),
        "Return": st.column_config.NumberColumn(format="%.4fx", help="Value / Gross (cash invested incl. fees)"),
        "Gain": st.column_config.NumberColumn(format="%+.2f%%"),
        "USD Value": st.column_config.NumberColumn(format="%.2f"),
        "USD Return": st.column_config.NumberColumn(format="%.4fx"),
        "USD Gain": st.column_config.NumberColumn(format="%+.2f%%")})

    left, right = st.columns([1, 1])
    with left:
        st.markdown("#### Cash balances")
        cash = T.cash_table(m)
        show_df(cash, column_config={"Net cash": st.column_config.NumberColumn(format="%.2f"),
                                     "USD Value": st.column_config.NumberColumn(format="%.2f")})
    with right:
        st.markdown("#### Allocation (USD)")
        alloc = [{"Holding": r.ticker, "USD": r.usd_value} for r in m.positions if abs(r.usd_value) > 1e-9]
        alloc += [{"Holding": f"Cash {r.currency}", "USD": r.usd_value} for r in m.cash if abs(r.usd_value) >= 0.005]
        if alloc:
            st.bar_chart(pd.DataFrame(alloc), x="Holding", y="USD", horizontal=True, **stretch())

    hist = T.snapshots_table(tracker_history(p.account))
    if len(hist) >= 2:
        st.markdown("#### Value history (recorded at each refresh)")
        st.line_chart(hist.rename(columns={"usd_total": "Total value (USD)", "usd_deposits": "Net deposits (USD)"}),
                      x="as_of", y=["Total value (USD)", "Net deposits (USD)"], **stretch())

    st.caption(f"Valuation date {md.as_of} · {md.mode}")


def tracker_history(account: str) -> list[dict]:
    return get_tracker().history(account)


# ======================================================================================================
# other tabs
# ======================================================================================================
def transactions(snap: Snapshot) -> None:
    m, p = snap.metrics, snap.portfolio
    base = m.base_currency
    t1, t2, t3, t4 = st.tabs(["1 · Stock transactions (+ 5 · extensions)", "2 · Dividends",
                              "3 · Deposits & withdrawals", "4 · Forex"])
    money = lambda: st.column_config.NumberColumn(format="%.2f")  # noqa: E731
    with t1:
        df = T.trades_table(m)
        show_df(df, column_config={
            "Quantity": st.column_config.NumberColumn(format="%.4f"), "Price": st.column_config.NumberColumn(format="%.4f"),
            "Total": money(), "Fee": st.column_config.NumberColumn(format="%.4f"), "Gross": money(),
            "Current Price": st.column_config.NumberColumn(format="%.2f"), "Current Total": money(),
            "USD Total": money(), "USD Gross": money(), "USD Current Total": money(),
            "VUAA Quantity": st.column_config.NumberColumn(format="%.4f"),
            "Date": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm:ss")}, height=420)
        st.caption("Sells carry negative quantity/total. Fee is a positive cost. USD columns use the closing rate of "
                   "the transaction day; VUAA Quantity needs benchmark prices (Refresh Prices).")
    with t2:
        show_df(T.dividends_table(p), column_config={
            "Quantity": st.column_config.NumberColumn(format="%.4f"), "Dividend": st.column_config.NumberColumn(format="%.5f"),
            "Total": money(), "Withholding Tax": money(), "Date": st.column_config.DateColumn(format="YYYY-MM-DD")})
        st.caption("Withholding Tax includes dividend-related fees (e.g. ADR fees). Dividend, tax and fee lines of "
                   "one payment are merged into one row.")
    with t3:
        show_df(T.deposits_table(m), column_config={
            "Amount": money(), f"USD.{base}": st.column_config.NumberColumn(format="%.4f"),
            "USD Amount": money(), f"Amount ({base})": money(), "Date": st.column_config.DateColumn(format="YYYY-MM-DD")})
        st.caption(f"USD.{base} = {base} per 1 USD on that day, so USD Amount = Amount / USD.{base}.")
    with t4:
        show_df(T.forex_table(p), column_config={
            "Quantity": st.column_config.NumberColumn(format="%.6f"), "Price": st.column_config.NumberColumn(format="%.5f"),
            "Total": money(), "Fee": st.column_config.NumberColumn(format="%.4f"),
            "Date": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm:ss")}, height=420)


def reconciliation(snap: Snapshot) -> None:
    p = snap.portfolio
    st.markdown("#### Cash, positions and NAV versus the newest statement")
    if snap.checks:
        verdict = overall(snap.checks)
        (st.success if verdict == "all checks passed" else st.warning)(verdict)
        df = T.checks_table(snap.checks)
        show_df(df, column_config={c: st.column_config.NumberColumn(format="%.6f") for c in
                                   ("IBKR statement", "Workbook", "Difference", "Tolerance")})
        st.caption("NAV differences are informational (INFO): the workbook values holdings at the latest prices, "
                   "IBKR's NAV is at the statement date.")
    else:
        st.info("The newest statement has no cash report / positions to compare with.")
    st.markdown("#### Coverage")
    cover = pd.DataFrame([{"File": x.filename, "Format": x.fmt.upper(), "From": x.period_start, "To": x.period_end}
                          for x in p.statements])
    show_df(cover)
    st.caption(f"{p.duplicates_dropped} duplicate transaction(s) found in overlapping statements were counted once.")
    problems = [*p.warnings, *snap.md.notes]
    for s_ in p.statements:
        problems += [f"{s_.filename}: {u}" for u in getattr(s_, "unsupported", [])]
    if problems:
        st.markdown("#### Warnings")
        for w in dict.fromkeys(problems):
            st.warning(w)


def market_data(tracker: Tracker, snap: Snapshot) -> None:
    p, md, s = snap.portfolio, snap.md, snap.settings
    st.caption(f"Mode: {md.mode} · valuation date {md.as_of}")
    for n in md.notes:
        st.warning(n)
    errors = st.session_state.get("refresh_errors") or {}
    if errors:
        with st.expander(f"Last refresh: {len(errors)} symbol(s) could not be downloaded"):
            show_df(pd.DataFrame({"Symbol": list(errors), "Problem": list(errors.values())}))
            st.caption("The cached price, or the closing price printed in the statement, is used for these symbols.")

    st.markdown("#### Stock prices")
    df = T.prices_table(md, p, s)
    edited = st.data_editor(
        df, hide_index=True, key="px_editor", disabled=[c for c in df.columns if c not in ("Yahoo symbol", "Override")],
        column_config={"Last close": st.column_config.NumberColumn(format="%.4f"),
                       "Price date": st.column_config.DateColumn(format="YYYY-MM-DD"),
                       "Override": st.column_config.NumberColumn(format="%.4f", help="Manual price - wins over "
                                                                  "downloaded and statement prices"),
                       "Yahoo symbol": st.column_config.TextColumn(help="Change when Yahoo uses another symbol "
                                                                    "(e.g. BRK-B, NOVO-B.CO)"),
                       "Price used": st.column_config.NumberColumn(format="%.4f")}, **stretch())

    st.markdown("#### FX rates (units of currency per 1 USD)")
    fx_df = T.fx_status_table(md, p, s)
    fx_edit = st.data_editor(
        fx_df, hide_index=True, key="fx_editor", disabled=[c for c in fx_df.columns if c != "Override"],
        column_config={"Last rate (per USD)": st.column_config.NumberColumn(format="%.4f"),
                       "Rate date": st.column_config.DateColumn(format="YYYY-MM-DD"),
                       "Override": st.column_config.NumberColumn(format="%.4f"),
                       "Rate used": st.column_config.NumberColumn(format="%.4f")}, **stretch())

    st.markdown("#### Benchmark")
    bm = md.benchmark.latest
    c1, c2 = st.columns([2, 1])
    with c1:
        st.write(f"`{s.get('benchmark_symbol')}`: " + (f"{bm.price:,.4f} USD on {bm.day} ({bm.source})" if bm
                                                          else "no data yet - press **Refresh Prices**."))
    with c2:
        bench_over = st.number_input("Manual benchmark price (USD)", value=s.get("benchmark_override"),
                                     placeholder="no override", key="w_bench_over", min_value=0.0, format="%.4f")

    if st.button("💾  Save symbols and overrides", key="save_over"):
        auto = lambda t, c: tracker.service.symbol_for(p, {}, t, c)                       # noqa: E731
        symbols = {}
        prices = {}
        for _, row in edited.iterrows():
            key = f"{row['Ticker']}|{row['Currency']}"
            sym = str(row["Yahoo symbol"] or "").strip()
            if sym and sym != auto(row["Ticker"], row["Currency"]):
                symbols[key] = sym
            if pd.notna(row["Override"]) and float(row["Override"]) > 0:
                prices[key] = float(row["Override"])
        fx_over = {r["Currency"]: float(r["Override"]) for _, r in fx_edit.iterrows()
                   if pd.notna(r["Override"]) and float(r["Override"]) > 0}
        tracker.update_settings(yahoo_symbols=symbols, price_overrides=prices, fx_overrides=fx_over,
                                benchmark_override=float(bench_over) if bench_over else None)
        flash("success", "Saved. Press Refresh Prices to download prices for changed symbols.")
        if s.get("export_excel"):
            post_outcome(tracker.export(p.account), "Overrides")
        st.rerun()

    with st.expander("Maintenance"):
        if st.button("Clear the downloaded price cache", key="clear_cache"):
            tracker.store.clear_market_cache()
            flash("info", "Market-data cache cleared. Press Refresh Prices to download it again.")
            st.rerun()


def archive(tracker: Tracker, account: str) -> None:
    st.markdown("#### Imported statements")
    rows = tracker.statements(account)
    if not rows:
        st.info("Nothing imported yet.")
        return
    st.caption("Every statement you upload is kept (parsed) in the local archive, so history survives restarts and "
               "older statements can be combined with newer ones. Re-uploading a file changes nothing.")
    for r in rows:
        c = st.columns([5, 1.2, 2.6, 2.4, 1.2, 1])
        c[0].write(f"**{r.filename}**")
        c[1].write(r.fmt.upper())
        c[2].write(f"{r.period_start} → {r.period_end}")
        c[3].write(f"imported {str(r.imported_at).replace('T', ' ')[:16]}")
        c[4].write(f"{r.n_trades} trades")
        if c[5].button("🗑", key=f"del_{r.id}", help=f"Remove {r.filename} from the archive"):
            tracker.delete_statement(r.id)
            flash("info", f"Removed {r.filename} from the archive.")
            if tracker.settings().get("export_excel") and tracker.statements(account):
                post_outcome(tracker.export(account), "Archive change")
            st.rerun()


def export_tab(tracker: Tracker, snap: Snapshot) -> None:
    p, s = snap.portfolio, snap.settings
    state, detail, path = tracker.workbook_status(p, s)
    st.markdown("#### Excel workbook")
    st.write(f"`{path}`")
    if state == "current":
        st.success(f"The workbook is up to date: {detail}. *Refresh Prices* only rewrites its market-data sheets.")
    elif state == "stale":
        st.warning(f"The workbook will be regenerated on the next export: {detail}. The old file is kept as "
                   f"{path.stem}.previous.xlsx.")
    else:
        st.info("No workbook has been generated yet.")
    if not s.get("export_excel"):
        st.caption("Automatic export is switched **off** (sidebar). Use the button below to generate the file once.")
    c1, c2 = st.columns(2)
    if c1.button("📄  Create / update workbook now", key="export_now", **stretch()):
        with st.spinner("Writing the workbook ..."):
            out = tracker.export(p.account)
        post_outcome(out, "Export")
        st.rerun()
    if path.exists():
        c2.download_button("⬇  Download .xlsx", data=path.read_bytes(), file_name=path.name, mime=XLSX_MIME,
                           key="download_xlsx", **stretch())
    st.markdown(
        "- **Formula driven:** totals, conversions, positions, cash, summaries and the benchmark simulation are Excel "
        "formulas; only statement records and market data are values.\n"
        "- **Refresh in place:** *Refresh Prices* updates the `Prices`, `FX`, `Benchmark` and `About` sheets and keeps "
        "everything else - including your yellow override cells - untouched.\n"
        "- **Checks sheet** reconciles cash, positions and NAV with the statement.")
    if path.exists():
        st.caption(f"Last written {datetime.fromtimestamp(path.stat().st_mtime):%Y-%m-%d %H:%M:%S}")


# ======================================================================================================
# page
# ======================================================================================================
def main() -> None:
    st.set_page_config(page_title="IBKR Portfolio Tracker", page_icon="📈", layout="wide",
                       initial_sidebar_state="expanded")
    st.markdown("<style>.block-container{padding-top:2.2rem;} div[data-testid='stMetricValue']{font-size:1.55rem;}</style>",
                unsafe_allow_html=True)
    tracker = get_tracker()
    account, settings, refresh = sidebar(tracker)

    if refresh and account:
        bar = st.sidebar.progress(0.0, text="Starting ...")

        def progress(i: int, n: int, sym: str) -> None:
            bar.progress(min(1.0, i / max(n, 1)), text=f"Downloading {sym} ..." if sym else "Recalculating ...")

        out = tracker.refresh_prices(account, progress)
        bar.empty()
        if out.report is not None and not out.report.offline:
            flash("success" if not out.report.errors else "warning", out.report.summary())
            st.session_state["refresh_errors"] = {sym: short_error(err) for sym, err in out.report.errors.items()}
        post_outcome(out, "Refresh")
        st.session_state["last_refresh_ui"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.rerun()

    st.title("Portfolio")
    show_flash()
    if not account:
        st.markdown(
            "### Welcome\n"
            "1. In the **IBKR Client Portal** open *Performance & Reports → Statements* and download an "
            "**Activity Statement** (Custom date range, format **CSV** and/or **HTML**).\n"
            "2. Drop the file(s) into the sidebar - any number, any mix of formats and periods.\n"
            "3. Press **Refresh Prices** to download market data, and switch on **Export results as Excel file** "
            "to get the formula-driven workbook.")
        return

    snap = tracker.snapshot(account, settings)
    if snap is None:
        st.info("No data for this account.")
        return
    p = snap.portfolio
    st.caption(f"{p.account}{(' · ' + p.alias) if p.alias else ''} · base currency {p.base_currency} · "
               f"statements {p.period_start} → {p.period_end} · {len(p.statements)} file(s) merged")
    if snap.problem:
        st.error(snap.problem)

    tabs = st.tabs(["Dashboard", "Transactions", "Reconciliation", "Market data", "Archive", "Excel export"])
    with tabs[0]:
        if snap.metrics is not None:
            dashboard(snap)
        else:
            st.info("The dashboard needs FX rates - see the Market data tab.")
    with tabs[1]:
        if snap.metrics is not None:
            transactions(snap)
    with tabs[2]:
        if snap.metrics is not None:
            reconciliation(snap)
    with tabs[3]:
        market_data(tracker, snap)
    with tabs[4]:
        archive(tracker, account)
    with tabs[5]:
        export_tab(tracker, snap)


if __name__ == "__main__":
    from streamlit.runtime import exists

    if exists():
        main()
    else:                                                   # started with "python app.py"
        from streamlit.web import cli as stcli

        sys.argv = ["streamlit", "run", str(Path(__file__).resolve()), *sys.argv[1:]]
        sys.exit(stcli.main())
