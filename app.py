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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ibkr_tracker import __version__                                   # noqa: E402
from ibkr_tracker import allocation as A                               # noqa: E402
from ibkr_tracker import analytics                                     # noqa: E402
from ibkr_tracker import tables as T                                   # noqa: E402
from ibkr_tracker.config import hide_closed_dust                       # noqa: E402
from ibkr_tracker.exports import (CSV_STYLE_LABELS, CSV_STYLES, csv_style, history_csv,  # noqa: E402
                                   risk_csv)
from ibkr_tracker.market.manual import BENCHMARK_KEY, PriceParseError, prices_to_text  # noqa: E402
from ibkr_tracker.market.symbols import yahoo_symbol                   # noqa: E402
from ibkr_tracker.portfolio import CONSOLIDATED, CONSOLIDATED_LABEL    # noqa: E402
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


# the price-coverage table: the status text is long - give it room (and the short columns little)
COVERAGE_COLUMNS = {"Status": st.column_config.TextColumn(width="large"),
                    "Ticker": st.column_config.TextColumn(width="small"),
                    "Currency": st.column_config.TextColumn(width="small")}


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
        why = f" - {out.update.reason}" if out.update.reason else ""
        verb = {"updated": "updated in place", "rebuilt": "rebuilt" + why, "created": "created"}[out.update.action]
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
                if st.session_state.get("account") != CONSOLIDATED:        # stay in the consolidated view if it is open
                    st.session_state["account"] = touched[0]
                    tracker.update_settings(last_account=touched[0])
                if any(r.is_new for r in results) and settings.get("export_excel"):
                    with st.spinner("Updating the Excel workbook ..."):
                        post_outcome(tracker.export(st.session_state["account"]), "Import")
            st.session_state["uploader_nonce"] = nonce + 1
            st.rerun()

        # ---- account --------------------------------------------------------------------------------
        accounts = tracker.accounts()
        account: Optional[str] = None
        if accounts:
            labels = {CONSOLIDATED: "∑  " + CONSOLIDATED_LABEL}
            labels.update({a: a + (f"  ·  {alias}" if alias else "") for a, alias, _ in accounts})
            options = tracker.account_choices()                 # the consolidated view first, then every account
            if st.session_state.get("account") not in options:
                st.session_state["account"] = tracker.default_account()
            account = st.selectbox(
                "Account / view", options, format_func=labels.get, key="account",
                help="Inspect one account on its own, or choose 'All accounts (consolidated)' to merge every account "
                     "into a single portfolio: positions, cash, performance and transaction histories.")
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
        hide = st.toggle("Hide closed positions & dust balances", value=hide_closed_dust(settings), key="w_hide",
                         help="Leaves out positions with 0 shares and cash balances whose absolute amount is below 0.5 "
                              "(in the balance's own currency) - on the dashboard (tables and allocation chart), in "
                              "the Excel workbook and in the CSV files. Display only: totals, returns and CAGR "
                              "still include them, and the transaction history and the reconciliation stay complete.")
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
            cons_base = st.text_input(
                "Base currency of the consolidated view", value=settings.get("consolidated_base", ""), max_chars=3,
                placeholder="auto", key="w_consbase",
                help="Currency of the 'base currency' summary when all accounts are merged. Empty = the accounts' "
                     "common base currency, or USD when they differ.")
            out_dir = st.text_input("Output folder (empty = <data folder>/output)", value=settings.get("output_dir", ""),
                                    key="w_outdir")
            st.caption(f"Data folder: `{tracker.store.path.parent}`  (set IBKR_TRACKER_HOME to change)")
            lo_ok = tracker.libreoffice_available()
            recalc = st.checkbox("Embed calculated values (needs LibreOffice)", value=bool(settings.get("recalc_with_libreoffice")) and lo_ok,
                                 disabled=not lo_ok, key="w_recalc",
                                 help="Excel recalculates the workbook anyway. Embedding values only makes the "
                                      "file readable in previewers that do not calculate."
                                      + ("" if lo_ok else " LibreOffice was not found on this computer."))
        new = dict(export_excel=bool(export), hide_closed_dust=bool(hide), price_source=source,
                   benchmark_symbol=bench.strip() or "VUAA.L",
                   days_per_year=float(days), output_dir=out_dir.strip(),
                   consolidated_base=(cons_base.strip().upper() if len(cons_base.strip()) == 3 else ""),
                   recalc_with_libreoffice=bool(recalc) if lo_ok else bool(settings.get("recalc_with_libreoffice")))
        if any(settings.get(k) != v for k, v in new.items()):
            was_on, was_hidden = bool(settings.get("export_excel")), hide_closed_dust(settings)
            settings = tracker.update_settings(**new)
            filter_changed = new["hide_closed_dust"] != was_hidden
            if new["export_excel"] and account and (not was_on or filter_changed):
                # just switched on, or the view filter changed (Sections 6 / 7 list other rows): write the file now
                with st.spinner("Rebuilding the Excel workbook ..." if filter_changed and was_on
                                else "Writing the Excel workbook ..."):
                    post_outcome(tracker.export(account), "Export")
    return account, settings, bool(refresh)


# ======================================================================================================
# dashboard
# ======================================================================================================
def secondary_benchmark_card(b, bench_label: str) -> None:
    """Small overview card under the KPI grid: the benchmark figures that are not part of the headline numbers
    (among them 'Stocks minus VUAA', which the primary slot gave up for the VUAA CAGR)."""
    def cell(col, label: str, value: str, tip: str = "") -> None:
        col.markdown(f"<div class='sec-label' title='{tip}'>{label}</div><div class='sec-value'>{value}</div>",
                     unsafe_allow_html=True)

    with st.container(border=True):
        st.caption("Benchmark comparison · details")
        c = st.columns(4)
        if b.available:
            cell(c[0], f"Stocks minus {bench_label} (USD)", fmt_money(b.excess_usd, "USD"),
                 "Value of the stock positions minus the simulated benchmark position.")
        else:
            cell(c[0], f"Stocks minus {bench_label} (USD)", "n/a", "Needs benchmark prices (Refresh Prices).")
        cell(c[1], "Stocks CAGR (USD, same basis)", fmt_pct(b.cagr_stocks, signed=False),
             "Stocks value / invested, annualised over the same purchase-weighted years as the benchmark CAGR.")
        cell(c[2], f"{bench_label} return (USD)", fmt_mult(b.ret_vuaa) if b.available else "n/a",
             "Simulated benchmark value / amount invested in stocks.")
        cell(c[3], "Avg. years since purchases", f"{b.years:.3f}" if b.years else "n/a",
             "Purchase-weighted average time from each stock transaction to the valuation date.")


def dashboard(snap: Snapshot, tracker: Tracker) -> None:
    m, md, p, s = snap.metrics, snap.md, snap.portfolio, snap.settings
    base = m.base_currency
    bench_label = (s.get("benchmark_symbol") or "benchmark").split(".")[0]
    hide = hide_closed_dust(s)                         # view filter: closed positions / dust balances are not listed

    c = st.columns(4)
    kpi(c[0], "Total value (USD)", fmt_money(m.usd_total, "USD"), help="Σ USD Value of positions + cash (Sections 6-8).")
    kpi(c[1], "Net deposits (USD)", fmt_money(m.usd_deposits, "USD"),
        help="Σ USD Amount of deposits, withdrawals and monthly fees (Section 3), converted at each day's rate.")
    kpi(c[2], "Return (USD)", fmt_mult(m.usd_return), fmt_pct(None if m.usd_return is None else m.usd_return - 1),
        help="Total value / net deposits.")
    kpi(c[3], "CAGR (USD)", fmt_pct(m.usd_cagr, signed=False),
        help="Time-weighted: Return^(1/years) - 1 with years = deposit-weighted average time since the deposits "
             f"({m.years:.3f} y)." if m.years else "n/a")
    if base != "USD":                                  # with a USD base this row would only repeat the one above
        c = st.columns(4)
        kpi(c[0], f"Total value ({base})", fmt_money(m.base_total, base, 0), help="Total USD value x latest USD rate.")
        kpi(c[1], f"Net deposits ({base})", fmt_money(m.base_deposits, base, 0))
        kpi(c[2], f"Return ({base})", fmt_mult(m.base_return),
            fmt_pct(None if m.base_return is None else m.base_return - 1),
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
        kpi(c[3], f"{bench_label} CAGR (USD)", fmt_pct(b.cagr_vuaa, signed=False),
            help=f"Time-weighted: (benchmark value / invested)^(1/years) - 1 in USD, years = purchase-weighted average "
                 f"time since the stock purchases ({b.years:.3f} y)." if b.years else
                 "Needs stock transactions and benchmark prices.")
    else:
        kpi(c[2], f"{bench_label} instead (USD)", "n/a")
        kpi(c[3], f"{bench_label} CAGR (USD)", "n/a", help="Needs benchmark prices: press Refresh Prices "
                                                            "(or enter a manual benchmark price).")
    secondary_benchmark_card(b, bench_label)

    if p.is_consolidated and snap.accounts:
        st.markdown("#### Accounts")
        acc = T.accounts_table(snap.accounts)
        for col in ("Share of total", "CAGR (USD)"):                       # fractions -> percent for display
            acc[col] = pd.to_numeric(acc[col], errors="coerce") * 100
        show_df(acc, column_config={
            "Stocks (USD)": st.column_config.NumberColumn(format="%.2f"),
            "Cash (USD)": st.column_config.NumberColumn(format="%.2f"),
            "Total value (USD)": st.column_config.NumberColumn(format="%.2f"),
            "Share of total": st.column_config.NumberColumn(format="%.1f%%"),
            "Net deposits (USD)": st.column_config.NumberColumn(format="%.2f"),
            "Return (USD)": st.column_config.NumberColumn(format="%.4fx"),
            "CAGR (USD)": st.column_config.NumberColumn(format="%.2f%%"),
            "Total value (account base)": st.column_config.NumberColumn(format="%.2f"),
            "Statement to": st.column_config.DateColumn(format="YYYY-MM-DD")})
        st.caption("Each account valued with the same prices and FX rates as the consolidated totals above; "
                   "'Total value (account base)' is in that account's own base currency.")

    st.markdown("#### Positions")
    pos = T.positions_table(m, hide)
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
    if hide and (note := T.hidden_note(m, hide, "closed")):
        st.caption(note)

    left, right = st.columns([1, 1])
    with left:
        st.markdown("#### Cash balances")
        cash = T.cash_table(m, hide)
        show_df(cash, column_config={"Net cash": st.column_config.NumberColumn(format="%.2f"),
                                     "USD Value": st.column_config.NumberColumn(format="%.2f")})
        if hide and (note := T.hidden_note(m, hide, "dust")):
            st.caption(note)
    with right:
        st.markdown("#### Allocation (USD)")
        alloc = [{"Holding": r.ticker, "USD": r.usd_value} for r in m.visible_positions(hide)
                 if abs(r.usd_value) > 1e-9]
        alloc += [{"Holding": f"Cash {r.currency}", "USD": r.usd_value} for r in m.visible_cash(hide)
                  if abs(r.usd_value) >= 0.005]
        if alloc:
            st.bar_chart(pd.DataFrame(alloc), x="Holding", y="USD", horizontal=True, **stretch())

    performance_history(snap)
    portfolio_analysis(tracker, snap)

    source = {"open_positions": "the Open Positions section of the latest statement",
              "open_positions+transactions": "the newest Open Positions section plus the later transactions",
              "transactions": "the sum of the transactions (no Open Positions section found)"}.get(p.holdings_source, "")
    st.caption(f"Valuation date {md.as_of} · {md.mode}" + (f" · share counts from {source}" if source else ""))


HISTORY_PERIODS = ["All", "1Y", "YTD", "6M", "3M", "1M"]


def period_start(end: date, period: str) -> Optional[date]:
    """First day of the chart window: ``All`` = the whole lifespan, ``YTD`` = 1 January of the valuation year."""
    if period == "YTD":
        return date(end.year, 1, 1)
    days = {"1Y": 365, "6M": 183, "3M": 91, "1M": 30}.get(period)
    return end - timedelta(days=days) if days else None


def performance_history(snap: Snapshot) -> None:
    """Charts of the day-by-day reconstruction of the whole lifespan (first transaction -> valuation date)."""
    h = snap.history()
    st.markdown("#### Performance since the first transaction")
    if h is None or h.empty:
        st.info("There is nothing to chart yet - the statements contain no transactions.")
        return
    base = h.base_currency
    bench = (h.benchmark_symbol or "benchmark").split(".")[0]
    c1, c2 = st.columns([1, 2])
    with c1:
        unit_choice = st.radio("Currency", [base, "USD"] if base != "USD" else ["USD"], horizontal=True,
                               key="hist_unit", help="The base currency valuation includes the currency effect: each "
                               "deposit is converted at the rate of its day, the value at the rate of every day.")
    with c2:
        period = st.radio("Period", HISTORY_PERIODS, horizontal=True, key="hist_period",
                          help="Chart window only - the figures themselves always start at the first transaction.")
    unit = "base" if (unit_choice == base and base != "USD") else "usd"
    since = period_start(h.end, period)
    ccy = base if unit == "base" else "USD"
    st.caption(f"Rebuilt day by day from the first transaction ({h.start}) to the valuation date ({h.end}) - "
               f"{h.days:,} days - from the transactions, the daily prices and the daily FX rates; it does not depend "
               "on when prices were refreshed. The last day equals the figures above.")
    if mismatch := h.matches(snap.metrics):
        st.warning("The last day of the history differs from the headline figures (" + "; ".join(mismatch) +
                   ") - a held stock probably has no price at all; see the data coverage below.")

    tabs = st.tabs(["Value & deposits", "Gain", "Return", "CAGR", f"vs. {bench}", "Allocation"])
    with tabs[0]:
        st.line_chart(T.history_chart_frame(h, "value", unit, since), height=330, **stretch())
        st.caption(f"Portfolio value (stocks + cash) against the net money paid in ({ccy}). The gap is the profit; "
                   "steps in the deposit line are deposits and withdrawals on their day.")
    with tabs[1]:
        st.line_chart(T.history_chart_frame(h, "gain", unit, since), height=330, **stretch())
        st.caption(f"Value minus net deposits ({ccy}).")
    with tabs[2]:
        st.line_chart(T.history_chart_frame(h, "return", unit, since), height=330, **stretch())
        st.caption("Value / net deposits - 1.0 means no profit yet, 1.25 means +25 %.")
    with tabs[3]:
        cagr = T.history_chart_frame(h, "cagr", unit, since) * 100
        cagr.columns = [f"{c} %" for c in cagr.columns]
        if cagr.dropna(how="all").empty:
            st.info("The annualised return is shown once the average deposit is three months old.")
        else:
            st.line_chart(cagr, height=330, **stretch())
        st.caption("Return^(1 / years) - 1 with the deposit-weighted average age of the deposits. Annualising a young "
                   "portfolio exaggerates - the curve starts after three months.")
    with tabs[4]:
        if not h.benchmark_available:
            st.info("The comparison needs benchmark prices: press Refresh Prices (or add a manual price history of "
                    "the benchmark in the Market data tab).")
        else:
            st.line_chart(T.history_chart_frame(h, "benchmark", unit, since), height=300, **stretch())
            st.caption(f"The stocks against what the same purchases (and sales) would be worth in {h.benchmark_symbol}, "
                       "replayed day by day in USD.")
            bcagr = T.history_chart_frame(h, "benchmark_cagr", unit, since) * 100
            bcagr.columns = [f"{c} %" for c in bcagr.columns]
            if not bcagr.dropna(how="all").empty:
                st.line_chart(bcagr, height=260, **stretch())
    with tabs[5]:
        st.area_chart(T.history_chart_frame(h, "allocation", unit, since), height=330, **stretch())
        st.caption(f"What the portfolio consisted of, every day ({ccy}).")

    cov = T.coverage_table(h)
    with st.expander(f"Data coverage of the history - {len(h.notes)} note(s)" if h.notes else
                     "Data coverage of the history", expanded=bool(h.notes)):
        for note in h.notes:
            st.warning(note)
        if len(cov):
            show_df(cov, column_config=COVERAGE_COLUMNS)
        if not h.notes:
            st.caption("Every day of every position is valued with a real price.")
        else:
            st.caption("Missing prices can be added under Market data > Manual price history; a stock Yahoo "
                       "does not know can get its own symbol under Market data > Ticker mapping.")
    data = history_csv(snap)
    if data:
        st.download_button("⬇  Download the daily history (CSV)", data=data,
                           file_name=f"IBKR_Portfolio_{snap.portfolio.account}_history.csv", mime="text/csv",
                           key="download_history_csv")


# ======================================================================================================
# portfolio analysis: allocation (donut chart) and risk & return
# ======================================================================================================
def theme_text_color() -> str:
    """Text colour for the centre of the donut: the active theme's (``st.context.theme`` - Streamlit 1.46+), else a mid
    grey that can be read on a light and on a dark background."""
    try:
        kind = st.context.theme.type
    except Exception:                                                  # noqa: BLE001 - older Streamlit / no browser yet
        kind = None
    return {"light": "#1F2933", "dark": "#F5F7FA"}.get(kind or "", "#8B93A0")


PALETTE = ["#4C78A8", "#F58518", "#54A24B", "#E45756", "#72B7B2", "#EECA3B", "#B279A2", "#FF9DA6", "#9D755D", "#BAB0AC"]
GROUP_LABELS = A.GROUPINGS


def donut_chart(slices: list[A.Slice], total: float, height: int = 340):
    """A donut chart of ``slices`` (Altair): the legend carries the weights, the hole the total, the tooltip the details."""
    import altair as alt

    legend = [f"{s.label}  ·  {s.weight * 100:.1f}%" for s in slices]
    df = pd.DataFrame({"Group": [s.label for s in slices], "Legend": legend, "USD": [s.usd_value for s in slices],
                       "Weight": [s.weight for s in slices]})
    outer = int(height * 0.46)
    arcs = alt.Chart(df).mark_arc(innerRadius=int(outer * 0.58), outerRadius=outer, padAngle=0.012,
                                  cornerRadius=2).encode(
        theta=alt.Theta("USD:Q", stack=True),
        color=alt.Color("Legend:N", sort=legend, scale=alt.Scale(domain=legend, range=PALETTE[:len(legend)]),
                        legend=alt.Legend(title=None, orient="right", symbolType="circle", symbolSize=170,
                                          labelFontSize=13, labelLimit=330, rowPadding=5)),
        order=alt.Order("USD:Q", sort="descending"),
        tooltip=[alt.Tooltip("Group:N"), alt.Tooltip("USD:Q", format=",.2f", title="USD value"),
                 alt.Tooltip("Weight:Q", format=".1%")])
    ink = theme_text_color()
    hole = alt.Chart(pd.DataFrame({"x": [0]})).mark_text(fontSize=22, fontWeight="bold", dy=-9, color=ink).encode(
        text=alt.value(f"{total:,.0f}"))
    unit = alt.Chart(pd.DataFrame({"x": [0]})).mark_text(fontSize=13, dy=13, color=ink).encode(
        text=alt.value("USD in total"))
    return (arcs + hole + unit).properties(height=height).configure_view(stroke=None)


def allocation_view(tracker: Tracker, snap: Snapshot) -> None:
    holdings = snap.holdings()
    if not holdings:
        st.info("There is nothing to allocate - the portfolio holds no stocks and no cash.")
        return
    st.caption("How the portfolio is split - shares of the positive USD values of the stocks and cash balances. Pick the "
               "grouping: asset class, stock type, industry sector, currency or single holding.")
    by = st.radio("Group by", list(GROUP_LABELS), format_func=GROUP_LABELS.get, horizontal=True, key="alloc_by")
    a = snap.allocation(by)
    if not a.slices:
        st.info("There are no positive values to chart.")
    else:
        left, right = st.columns([3, 2])
        with left:
            st.altair_chart(donut_chart(A.chart_slices(a), a.total), **stretch())
        with right:
            tab = T.allocation_table(a)
            tab["Weight"] = tab["Weight"] * 100
            show_df(tab.drop(columns=["Members"]), column_config={
                "USD Value": st.column_config.NumberColumn(format="%.2f"),
                "Weight": st.column_config.NumberColumn(format="%.1f%%"),
                "Holdings": st.column_config.NumberColumn(format="%d", width="small")}, height=min(35 * (len(tab) + 1) + 3, 420))
            st.caption(f"Total {a.total:,.2f} USD = 100 %.")
        if len(a.slices) > len(A.chart_slices(a)):
            st.caption("The smallest groups are merged into *Other* in the chart; the table lists every one.")
    if a.excluded:
        st.caption("Not in the chart (a negative value cannot be a slice): " +
                   ", ".join(f"{t} {v:,.2f} USD" for t, v in a.excluded) + ".")
    if by == "sector" and a.unclassified_share > 0:
        st.info(f"{a.unclassified_share * 100:.1f}% of the portfolio has no sector yet. IBKR statements carry no sector "
                "information: press *Look up sectors* below (Yahoo Finance) or type the sector in yourself.")

    c = A.concentration(holdings)
    cols = st.columns(5)
    tips = {"Stock positions": "Stocks held now.", "Effective positions":
            "1 / Σ weight² of the stock weights: how many equally large positions would be as concentrated as this "
            "portfolio (10 equal positions = 10.0; one dominant position = close to 1).",
            "Top 3 positions": "Share of the three largest stocks in the whole portfolio (stocks + cash)."}
    for col, (label, value, kind) in zip(cols, T.concentration_cards(c)):
        text = ("n/a" if value is None else f"{value * 100:.1f}%" if kind == "pct" else
                f"{value:,.0f}" if kind == "count" else f"{value:,.2f}")
        kpi(col, label, text, help=tips.get(label, "Share of the whole portfolio (stocks + cash)." if kind == "pct" else None))
    classification_editor(tracker, snap)


def classification_editor(tracker: Tracker, snap: Snapshot) -> None:
    p, s = snap.portfolio, snap.settings
    holdings = snap.holdings()
    stocks = [h for h in holdings if not h.is_cash]
    if not stocks:
        return
    online = s.get("price_source", "yahoo") == "yahoo"
    with st.expander("Classify your holdings - asset class and industry sector", expanded=False):
        st.caption("The asset class comes from the instrument type in your statement (an ETF or fund is *ETFs & funds*, "
                   "everything else *Stocks*); the sector from Yahoo Finance (*Look up sectors*) - funds have none. "
                   "Type another value into **Asset class** or **Sector** to override it; an entry that equals the "
                   "automatic value is not kept. Sector names follow Yahoo's: " + ", ".join(A.SECTORS) + ".")
        nonce = st.session_state.setdefault("class_nonce", 0)
        df = T.classification_table(holdings)
        df["Weight"] = df["Weight"] * 100
        options = list(dict.fromkeys([*A.ASSET_CLASSES, *df["Asset class"]]))
        edited = st.data_editor(
            df, hide_index=True, key=f"class_editor_{nonce}",
            disabled=[c for c in df.columns if c not in ("Asset class", "Sector")],
            column_config={
                "Asset class": st.column_config.SelectboxColumn(options=options, required=True),
                "Sector": st.column_config.TextColumn(help="Empty = the automatic sector (Yahoo Finance, or 'ETFs & "
                                                           "funds' / 'Unclassified')."),
                "USD Value": st.column_config.NumberColumn(format="%.2f"),
                "Weight": st.column_config.NumberColumn(format="%.1f%%"),
                "Ticker": st.column_config.TextColumn(width="small"), "Currency": st.column_config.TextColumn(width="small"),
                "Statement type": st.column_config.TextColumn(width="small")}, **stretch())
        b1, b2, b3 = st.columns([1, 1.4, 1.6])
        force = b3.checkbox("Ask Yahoo again for stocks that already have a sector", value=False, key="class_force",
                            disabled=not online)
        if b1.button("💾  Save classification", key="class_save", type="primary"):
            edits = {f"{r['Ticker']}|{r['Currency']}": ("" if pd.isna(r["Asset class"]) else str(r["Asset class"]),
                                                        "" if pd.isna(r["Sector"]) else str(r["Sector"]))
                     for _, r in edited.iterrows()}
            change = tracker.save_classification(holdings, edits)
            for e in change.errors:
                flash("error", e)
            if change.changed:
                flash("success", f"Classification saved ({len(change.changed)} holding(s) changed).")
                if s.get("export_excel"):
                    post_outcome(tracker.export(p.account), "Classification")
            elif not change.errors:
                flash("info", "The classification is unchanged.")
            st.session_state["class_nonce"] = nonce + 1
            st.rerun()
        if b2.button("🔎  Look up sectors (Yahoo Finance)", key="class_lookup", disabled=not online,
                     help="Asks Yahoo Finance for the sector of every stock you hold and remembers the answers. "
                          "Press *Refresh Prices* never does this by itself."):
            bar, cb = progress_bar("Looking up ...")
            out = tracker.lookup_profiles(p.account, progress=cb, force=bool(force))
            bar.empty()
            report = out.report
            if report is not None:
                flash("success" if (report.ok and not report.no_sector) else "warning", report.summary())
                for m in report.messages:
                    flash("warning", m)
                for key, err in report.errors.items():
                    flash("warning", f"{key.split('|')[0]}: {short_error(err)}")
                if report.found and s.get("export_excel"):
                    post_outcome(tracker.export(p.account), "Sector lookup")
            for e in out.errors:
                if report is None:
                    flash("error", e)
            st.session_state["class_nonce"] = nonce + 1
            st.rerun()
        if not online:
            st.caption("Offline mode (statements only) is on - sectors cannot be looked up; type them in instead.")


def risk_view(tracker: Tracker, snap: Snapshot) -> None:
    h = snap.history()
    if h is None or h.empty:
        st.info("There is nothing to analyse yet - the statements contain no transactions.")
        return
    base = h.base_currency
    c1, c2, c3 = st.columns([1.1, 2.2, 1.1])
    with c1:
        unit_choice = st.radio("Currency", [base, "USD"] if base != "USD" else ["USD"], horizontal=True, key="risk_unit",
                               help="The base-currency view includes the currency effect (a falling dollar is a loss "
                                    "for a euro or forint investor); the USD view is the portfolio's own movement.")
    with c2:
        period = st.radio("Period", HISTORY_PERIODS, horizontal=True, key="risk_period",
                          help="The window the figures are computed over (the volatility of the last 3 months is not "
                               "the volatility of the whole lifespan).")
    unit = "base" if (unit_choice == base and base != "USD") else "usd"
    ccy = base if unit == "base" else "USD"
    saved = analytics.risk_free_rate(snap.settings, ccy) * 100.0
    with c3:
        rf = st.number_input(f"Risk-free rate ({ccy}, % p.a.)", min_value=-50.0, max_value=100.0, value=float(saved),
                             step=0.25, format="%.2f", key=f"risk_rf_{ccy}",
                             help="The return you could have had without risk (a government bill or a savings account). "
                                  "Used by Sharpe, Sortino, alpha and Treynor; remembered per currency and written to the "
                                  "workbook's Risk sheet at its next update. 0 = the plain ratio of return to risk.")
    if abs(rf - saved) > 1e-9:
        tracker.set_risk_free(ccy, rf)
    rep = snap.risk(unit, period_start(h.end, period), rf / 100.0)
    st.caption(f"Time-weighted daily returns of the day-by-day history ({ccy}): deposits and withdrawals are taken out, so "
               "the figures measure the investing, not the timing of the payments. Annualised with 252 trading days.")
    if rep.status in (analytics.NO_HISTORY, analytics.NO_PRICES):
        st.info(rep.message)
        return
    if rep.status == analytics.FEW_DAYS:
        st.info(rep.message)
    for note in rep.notes:
        st.warning(note)
    p, b = rep.portfolio, rep.benchmark
    bench = (rep.benchmark_symbol or "benchmark").split(".")[0]
    if rep.status == analytics.OK:
        r1 = st.columns(4)
        kpi(r1[0], "Annualised volatility", fmt_pct(p.volatility, signed=False),
            help="Standard deviation of the daily returns x √252 - the typical yearly swing." +
                 (f" {bench}: {fmt_pct(b.volatility, signed=False)}." if b and b.volatility is not None else ""))
        kpi(r1[1], "Maximum drawdown", fmt_pct(p.max_drawdown, signed=False),
            help="The largest fall from a previous high. " + (analytics.drawdown_period(p) or "") +
                 (f" {bench}: {fmt_pct(b.max_drawdown, signed=False)}." if b and b.max_drawdown is not None else ""))
        kpi(r1[2], "Sharpe ratio", "n/a" if p.sharpe is None else f"{p.sharpe:.2f}",
            help="(Mean daily return - risk-free) / volatility, annualised. Above 1 is good, above 2 very good." +
                 (f" {bench}: {b.sharpe:.2f}." if b and b.sharpe is not None else ""))
        kpi(r1[3], "Sortino ratio", "n/a" if p.sortino is None else f"{p.sortino:.2f}",
            help="Like Sharpe, but only the days below the risk-free rate count as risk.")
        r2 = st.columns(4)
        kpi(r2[0], "Time-weighted return", fmt_pct(p.cumulative),
            None if p.annualized is None else f"{fmt_pct(p.annualized, signed=False)} a year",
            help="What the money at work earned, deposits and withdrawals removed.")
        kpi(r2[1], "Calmar ratio", "n/a" if p.calmar is None else f"{p.calmar:.2f}",
            help="Annualised return / |maximum drawdown| - return per unit of the worst fall.")
        rel = rep.relative
        kpi(r2[2], f"Beta vs {bench}", "n/a" if rel is None or rel.beta is None else f"{rel.beta:.2f}",
            help="1.2 = the portfolio moved 20 % more than the benchmark on a typical day." if rel else
                 "Needs benchmark prices (Refresh Prices).")
        kpi(r2[3], "Daily Value at Risk (95%)", fmt_pct(p.var95, signed=False),
            help="On 95 % of the days the loss was smaller than this (historical simulation, 1 day).")
    else:
        r1 = st.columns(2)
        kpi(r1[0], "Time-weighted return", fmt_pct(p.cumulative))
        kpi(r1[1], "Maximum drawdown", fmt_pct(p.max_drawdown, signed=False))

    table = T.risk_table(rep)
    if len(table):
        numbers = table[table["Metric"] != analytics.DRAWDOWN_PERIOD_LABEL]
        for group in dict.fromkeys(numbers["Group"]):
            part = numbers[numbers["Group"] == group].drop(columns=["Group"])
            st.markdown(f"**{group}**")
            show_df(part, column_config={"Metric": st.column_config.TextColumn(width="medium"),
                                         "Portfolio": st.column_config.TextColumn(width="small"),
                                         part.columns[2]: st.column_config.TextColumn(width="small"),
                                         "What it means": st.column_config.TextColumn(width="large")},
                    height=35 * (len(part) + 1) + 3)
        if p.mdd_peak is not None:
            st.caption(f"Maximum drawdown of the portfolio: {analytics.drawdown_period(p)}." +
                       (f" {bench}: {analytics.drawdown_period(b)}." if b is not None and b.mdd_peak is not None else ""))
    if rep.status == analytics.OK and not rep.frame.empty:
        tabs = st.tabs(["Drawdown", f"Rolling volatility ({analytics.ROLLING_WINDOW} days)", "Return distribution"])
        with tabs[0]:
            st.line_chart(T.risk_chart_frame(rep, "drawdown"), height=300, **stretch())
            st.caption("Distance (%) from the highest point of the time-weighted index so far - 0 = at a new high.")
        with tabs[1]:
            vol = T.risk_chart_frame(rep, "rolling_vol")
            if vol.dropna(how="all").empty:
                st.info(f"The rolling volatility needs {analytics.ROLLING_WINDOW} trading days.")
            else:
                st.line_chart(vol, height=300, **stretch())
                st.caption(f"Annualised volatility (%) of the last {analytics.ROLLING_WINDOW} trading days.")
        with tabs[2]:
            import altair as alt

            returns = pd.DataFrame({"Daily return (%)": rep.frame["ret"].to_numpy() * 100.0})
            hist = alt.Chart(returns).mark_bar().encode(
                x=alt.X("Daily return (%):Q", bin=alt.Bin(maxbins=40)), y=alt.Y("count()", title="Days"),
                tooltip=[alt.Tooltip("count()", title="Days")])
            var_line = alt.Chart(pd.DataFrame({"x": [-(p.var95 or 0.0) * 100.0]})).mark_rule(
                color="#E45756", strokeDash=[5, 4], size=2).encode(x="x:Q")
            st.altair_chart((hist + var_line).properties(height=300), **stretch())
            st.caption("How often each daily return occurred; the dashed line is the 95 % Value at Risk.")
    data = risk_csv(snap, unit, period_start(h.end, period), rf / 100.0)
    if data:
        st.download_button("⬇  Download these risk metrics (CSV)", data=data,
                           file_name=f"IBKR_Portfolio_{snap.portfolio.account}_risk_{ccy}.csv", mime="text/csv",
                           key="download_risk_csv")


def portfolio_analysis(tracker: Tracker, snap: Snapshot) -> None:
    """Allocation (donut chart) and risk & return - the part of the dashboard below the performance charts."""
    st.markdown("#### Portfolio analysis")
    tab_alloc, tab_risk = st.tabs(["Allocation breakdown", "Risk & return"])
    with tab_alloc:
        allocation_view(tracker, snap)
    with tab_risk:
        risk_view(tracker, snap)


# ======================================================================================================
# other tabs
# ======================================================================================================
def transactions(snap: Snapshot) -> None:
    m, p = snap.metrics, snap.portfolio
    base = m.base_currency
    acct = p.is_consolidated                       # consolidated view: first column = account of the record
    t1, t2, t3, t4 = st.tabs(["1 · Stock transactions (+ 5 · extensions)", "2 · Dividends",
                              "3 · Deposits & withdrawals", "4 · Forex"])
    money = lambda: st.column_config.NumberColumn(format="%.2f")  # noqa: E731
    with t1:
        df = T.trades_table(m, acct)
        show_df(df, column_config={
            "Quantity": st.column_config.NumberColumn(format="%.4f"), "Price": st.column_config.NumberColumn(format="%.4f"),
            "Total": money(), "Fee": st.column_config.NumberColumn(format="%.4f"), "Gross": money(),
            "Current Price": st.column_config.NumberColumn(format="%.2f"), "Current Total": money(),
            "USD Total": money(), "USD Gross": money(), "USD Current Total": money(),
            "VUAA Quantity": st.column_config.NumberColumn(format="%.4f"),
            "Date": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm:ss")}, height=420)
        st.caption("Sells carry negative quantity/total. Fee is a positive cost. USD columns use the closing rate of "
                   "the transaction day; VUAA Quantity needs benchmark prices (Refresh Prices). Current Total is "
                   "expressed in today's shares (statement share count / transaction sum, see Positions)."
                   + (" The Account column shows where each record comes from." if acct else ""))
    with t2:
        show_df(T.dividends_table(p, acct), column_config={
            "Quantity": st.column_config.NumberColumn(format="%.4f"), "Dividend": st.column_config.NumberColumn(format="%.5f"),
            "Total": money(), "Withholding Tax": money(), "Date": st.column_config.DateColumn(format="YYYY-MM-DD")})
        st.caption("Withholding Tax includes dividend-related fees (e.g. ADR fees). Dividend, tax and fee lines of "
                   "one payment are merged into one row.")
    with t3:
        show_df(T.deposits_table(m, acct), column_config={
            "Amount": money(), f"USD.{base}": st.column_config.NumberColumn(format="%.4f"),
            "USD Amount": money(), f"Amount ({base})": money(), "Date": st.column_config.DateColumn(format="YYYY-MM-DD")})
        st.caption(f"USD.{base} = {base} per 1 USD on that day, so USD Amount = Amount / USD.{base}.")
    with t4:
        show_df(T.forex_table(p, acct), column_config={
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
                   "IBKR's NAV is at the statement date. The share counts are taken from the statement's Open "
                   "Positions section; 'Share count' rows only show where the sum of the transactions differs "
                   "(INFO: stock split, transfer, earlier history).")
    else:
        st.info("The newest statement has no cash report / positions to compare with.")
    if p.is_consolidated and snap.account_checks:
        st.markdown("#### Per account")
        for acc, rows in snap.account_checks.items():
            verdict = overall(rows) if rows else "nothing to compare"
            with st.expander(f"{acc} - {verdict}", expanded=verdict not in ("all checks passed", "nothing to compare")):
                if rows:
                    show_df(T.checks_table(rows), column_config={c: st.column_config.NumberColumn(format="%.6f") for c in
                                                                 ("IBKR statement", "Workbook", "Difference", "Tolerance")})
    st.markdown("#### Coverage")
    cover = pd.DataFrame([{**({"Account": x.account} if p.is_consolidated else {}), "File": x.filename,
                           "Format": x.fmt.upper(), "From": x.period_start, "To": x.period_end} for x in p.statements])
    show_df(cover)
    st.caption(f"{p.duplicates_dropped} duplicate transaction(s) found in overlapping statements were counted once.")
    problems = [*p.warnings, *snap.md.notes]
    for s_ in p.statements:
        problems += [f"{s_.filename}: {u}" for u in getattr(s_, "unsupported", [])]
    if problems:
        st.markdown("#### Warnings")
        for w in dict.fromkeys(problems):
            st.warning(w)
    if p.notes:
        st.markdown("#### Notes")
        for n in dict.fromkeys(p.notes):
            st.info(n)


def progress_bar(label: str):
    """A progress bar and the callback the downloader reports to."""
    bar = st.progress(0.0, text=label)

    def callback(i: int, n: int, sym: str) -> None:
        bar.progress(min(1.0, i / max(n, 1)), text=f"Downloading {sym} ..." if sym else "Done")

    return bar, callback


def market_data(tracker: Tracker, snap: Snapshot) -> None:
    p, md, s = snap.portfolio, snap.md, snap.settings
    st.caption(f"Mode: {md.mode} · valuation date {md.as_of}")
    for n in md.notes:
        st.warning(n)
    errors = st.session_state.get("refresh_errors") or {}
    if errors:
        with st.expander(f"Last refresh: {len(errors)} symbol(s) could not be downloaded"):
            show_df(pd.DataFrame({"Symbol": list(errors), "Problem": list(errors.values())}))
            st.caption("The cached price, the manual price history or the closing price printed in the statement is "
                       "used for these symbols. A symbol Yahoo does not know under this name can be corrected in the "
                       "Ticker mapping tab; a stock it does not know at all gets its prices in Manual price history.")

    t_px, t_map, t_manual, t_cov = st.tabs(["Prices & overrides", "Ticker mapping", "Manual price history",
                                            "Price coverage"])
    with t_px:
        prices_overrides(tracker, snap)
    with t_map:
        ticker_mapping(tracker, snap)
    with t_manual:
        manual_price_history(tracker, snap)
    with t_cov:
        price_coverage(snap)

    with st.expander("Maintenance"):
        if st.button("Clear the downloaded price cache", key="clear_cache"):
            tracker.store.clear_market_cache()
            flash("info", "Market-data cache cleared. Press Refresh Prices to download it again. Your ticker mapping "
                          "and manual prices were not touched.")
            st.rerun()
        st.caption("The cache holds only what was downloaded. Ticker aliases, the manual price history and the "
                   "override prices are yours and stay.")


# ---- tab 1: current prices, FX, benchmark -----------------------------------------------------------------------
def prices_overrides(tracker: Tracker, snap: Snapshot) -> None:
    p, md, s = snap.portfolio, snap.md, snap.settings
    st.markdown("#### Stock prices")
    df = T.prices_table(md, p, s)
    edited = st.data_editor(
        df, hide_index=True, key="px_editor", disabled=[c for c in df.columns if c != "Override"],
        column_config={"Last close": st.column_config.NumberColumn(format="%.4f"),
                       "Price date": st.column_config.DateColumn(format="YYYY-MM-DD"),
                       "Override": st.column_config.NumberColumn(format="%.4f", help="One manual price for today's "
                                                                  "value - wins over downloaded, manual-history and "
                                                                  "statement prices"),
                       "Yahoo symbol": st.column_config.TextColumn(help="Change it in the Ticker mapping tab"),
                       "Price used": st.column_config.NumberColumn(format="%.4f")}, **stretch())
    st.caption("An **override** replaces today's price only. For a whole price history (a stock Yahoo does not know, "
               "or gaps in it) use *Manual price history*; to point a stock at the right Yahoo symbol use "
               "*Ticker mapping*.")

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

    if st.button("💾  Save overrides", key="save_over"):
        # the table shows this account's stocks only: overrides of other accounts' stocks are kept as they are
        shown = {f"{t}|{c}" for t, c in p.positions_keys()}
        prices = {k: v for k, v in (s.get("price_overrides") or {}).items() if k not in shown}
        for _, row in edited.iterrows():
            if pd.notna(row["Override"]) and float(row["Override"]) > 0:
                prices[f"{row['Ticker']}|{row['Currency']}"] = float(row["Override"])
        shown_fx = set(fx_edit["Currency"])
        fx_over = {k: v for k, v in (s.get("fx_overrides") or {}).items() if k not in shown_fx}
        fx_over.update({r["Currency"]: float(r["Override"]) for _, r in fx_edit.iterrows()
                        if pd.notna(r["Override"]) and float(r["Override"]) > 0})
        tracker.update_settings(price_overrides=prices, fx_overrides=fx_over,
                                benchmark_override=float(bench_over) if bench_over else None)
        flash("success", "Overrides saved.")
        if s.get("export_excel"):
            post_outcome(tracker.export(p.account), "Overrides")
        st.rerun()


# ---- tab 2: ticker mapping ---------------------------------------------------------------------------------------
def report_mapping(out) -> None:
    """Queue the messages of a saved / imported mapping."""
    for m in out.messages:
        flash("success", m)
    for e in out.errors:
        flash("error", e)
    if out.report is not None:
        for sym, err in out.report.errors.items():
            flash("warning", f"{sym}: {short_error(err)}")
        if out.report.errors:
            st.session_state["refresh_errors"] = {sym: short_error(err) for sym, err in out.report.errors.items()}
    if not out.changed and not out.errors:
        flash("info", "The mapping is unchanged.")


def ticker_mapping(tracker: Tracker, snap: Snapshot) -> None:
    p, s = snap.portfolio, snap.settings
    online = s.get("price_source", "yahoo") == "yahoo"
    st.markdown("#### Ticker mapping - IBKR ticker → Yahoo Finance symbol")
    st.caption(
        "Prices are downloaded under the **symbol used**. The automatic rule (listing exchange → suffix, "
        "e.g. `NOVOB` on CPH → `NOVOB.CO`) fits most stocks. When Yahoo Finance calls a stock differently, type the "
        "right symbol as **alias** - it is saved permanently and used for every download, workbook and account. "
        "Tick **manual prices only** for a stock Yahoo does not know at all: it is never downloaded and you provide "
        "its prices in the *Manual price history* tab.")
    rows = tracker.mapping_rows(p, s)
    if not rows:
        st.info("This portfolio holds no stocks.")
        return
    nonce = st.session_state.setdefault("map_nonce", 0)
    df = T.mapping_table(rows)
    edited = st.data_editor(
        df, hide_index=True, key=f"map_editor_{nonce}",
        disabled=[c for c in df.columns if c not in ("Your symbol (alias)", "Manual prices only")],
        column_config={
            "Your symbol (alias)": st.column_config.TextColumn(help="Yahoo Finance symbol, e.g. BRK-B, NOVO-B.CO, "
                                                                     "0700.HK. Empty = use the automatic symbol."),
            "Manual prices only": st.column_config.CheckboxColumn(help="Never download this stock - use the manual "
                                                                        "price history only."),
            "Held now": st.column_config.CheckboxColumn(),
            "Currency": st.column_config.TextColumn(width="small"), "Ticker": st.column_config.TextColumn(width="small"),
            "Status": st.column_config.TextColumn(width="large")}, **stretch())
    c1, c2 = st.columns([1, 2])
    download = c2.checkbox("Download the prices of the changed symbols right away", value=online, disabled=not online,
                           key="map_download")
    if c1.button("💾  Save mapping", key="map_save", type="primary"):
        edits = {f"{r['Ticker']}|{r['Currency']}": ("" if pd.isna(r["Your symbol (alias)"])
                                                   else str(r["Your symbol (alias)"]),
                                                   bool(r["Manual prices only"])) for _, r in edited.iterrows()}
        bar, cb = progress_bar("Saving ...")
        out = tracker.save_mapping(p, edits, download=bool(download) and online, progress=cb)
        bar.empty()
        report_mapping(out)
        if out.changed and s.get("export_excel"):
            post_outcome(tracker.export(p.account), "Ticker mapping")
        st.session_state["map_nonce"] = nonce + 1
        st.rerun()

    with st.expander("Test a symbol, then use it", expanded=False):
        st.caption("Asks Yahoo Finance for the last two weeks of the symbol: does it exist, what does it cost and in "
                   "which currency is it quoted?")
        labels = {f"{r.ticker} ({r.currency})": r for r in rows}
        c1, c2 = st.columns(2)
        label = c1.selectbox("Stock", list(labels), key="map_test_stock")
        symbol = c2.text_input("Yahoo Finance symbol", key="map_test_symbol", placeholder="e.g. BRK-B")
        row = labels[label]
        b1, b2, _ = st.columns([1, 1, 2])
        if b1.button("🔎  Test symbol", key="map_test", disabled=not online):
            res = tracker.check_symbol(symbol, row.currency)
            (st.success if res.ok and res.currency_ok else st.warning if res.ok else st.error)(res.message)
        if b2.button("✔  Use this symbol", key="map_use"):
            bar, cb = progress_bar("Saving ...")
            out = tracker.save_mapping(p, {row.key: (symbol, row.manual_only)}, download=online, progress=cb)
            bar.empty()
            report_mapping(out)
            if out.changed and s.get("export_excel"):
                post_outcome(tracker.export(p.account), "Ticker mapping")
            st.session_state["map_nonce"] = nonce + 1
            st.rerun()
        if not online:
            st.caption("Offline mode (statements only) is on - symbols cannot be tested.")

    with st.expander("Mapping file - export / import", expanded=False):
        st.caption("The mapping is stored with the settings in the data folder. Export it as a small CSV file "
                   "(`Ticker, Currency, Symbol, Manual only`) to back it up, edit it in a spreadsheet or use it on "
                   "another computer; an imported file is merged into the saved mapping.")
        st.download_button("⬇  Download ticker_mapping.csv", data=tracker.mapping_csv().encode("utf-8-sig"),
                           file_name="ticker_mapping.csv", mime="text/csv", key="map_download_csv")
        up = st.file_uploader("Import a mapping file", type=["csv", "txt"], key=f"map_upload_{nonce}")
        if up is not None and st.button("Import the file", key="map_import"):
            try:
                text = up.getvalue().decode("utf-8-sig")
            except UnicodeDecodeError:
                text = up.getvalue().decode("cp1252", errors="replace")
            bar, cb = progress_bar("Importing ...")
            out = tracker.import_mapping(p, text, download=online, progress=cb)
            bar.empty()
            report_mapping(out)
            if out.changed and s.get("export_excel"):
                post_outcome(tracker.export(p.account), "Ticker mapping")
            st.session_state["map_nonce"] = nonce + 1
            st.rerun()


# ---- tab 3: manual price history ---------------------------------------------------------------------------------
def manual_price_history(tracker: Tracker, snap: Snapshot) -> None:
    p, s = snap.portfolio, snap.settings
    st.markdown("#### Manual price history - `Date, Price`")
    st.caption(
        "For a stock Yahoo Finance does not know - or only partly - add its closing prices yourself: paste them, "
        "upload a CSV file or type them in. They are merged with the downloaded prices (on a day that both have, "
        "**yours wins**), set the current price when they are the newest, and value the stock on every day of the "
        "performance history. Prices are in the stock's **trading currency** and split-adjusted (like Yahoo's Close). "
        "Manual prices are never removed by *Clear the downloaded price cache*.")
    summary = tracker.manual_price_summary()
    bench_sym = s.get("benchmark_symbol") or "benchmark"
    show_df(T.manual_prices_table(p, summary, BENCHMARK_KEY, bench_sym))

    targets = {f"{t} ({c})": f"{t}|{c}" for t, c in p.positions_keys()}
    targets[f"Benchmark {bench_sym} (in its quote currency)"] = BENCHMARK_KEY
    nonce = st.session_state.setdefault("mp_nonce", 0)
    label = st.selectbox("Add prices for", list(targets), key="mp_target")
    key = targets[label]
    existing = tracker.manual_prices(key)
    if existing:
        st.caption(f"{len(existing)} manual price(s) saved for {label}: {existing[0][0]} → {existing[-1][0]}.")
    else:
        st.caption(f"No manual prices saved for {label} yet.")

    tab_paste, tab_file, tab_type = st.tabs(["Paste text", "Upload a file", "Type rows"])
    parsed, error, source = None, "", "pasted text"
    with st.expander("Options for unusual data", expanded=False):
        o1, o2 = st.columns(2)
        decimal = o1.selectbox("Decimal mark", ["auto", ".", ","], key=f"mp_decimal_{nonce}")
        order = o2.selectbox("Dates like 01/02/2026", ["auto", "day first", "month first"], key=f"mp_order_{nonce}")
    opts = {"decimal": None if decimal == "auto" else decimal,
            "date_order": {"auto": None, "day first": "dmy", "month first": "mdy"}[order]}
    with tab_paste:
        text = st.text_area("One price per line: a date and the closing price", height=170, key=f"mp_text_{nonce}",
                            placeholder="Date,Price\n2026-01-02,125.40\n2026-01-05,127.15",
                            help="Separators: comma, semicolon or tab. Dates such as 2026-01-02, 02.01.2026 or "
                                 "Jan 2, 2026. A header line is optional.")
    with tab_file:
        up = st.file_uploader("CSV or text file", type=["csv", "txt", "tsv"], key=f"mp_file_{nonce}",
                              help="Two columns Date, Price - or a Yahoo-style table with a Close column.")
    with tab_type:
        grid = st.data_editor(
            pd.DataFrame({"Date": pd.Series(dtype="datetime64[ns]"), "Price": pd.Series(dtype="float64")}),
            num_rows="dynamic", hide_index=True, key=f"mp_grid_{nonce}",
            column_config={"Date": st.column_config.DateColumn(format="YYYY-MM-DD", required=True),
                           "Price": st.column_config.NumberColumn(format="%.4f", min_value=0.0, required=True)},
            **stretch())
    try:
        if text and text.strip():
            parsed = tracker.parse_manual_prices(text, **opts)
        elif up is not None:
            parsed, source = tracker.parse_manual_prices_file(up.getvalue(), **opts), f"upload: {up.name}"
        elif len(grid.dropna()):
            lines = [f"{pd.Timestamp(d):%Y-%m-%d};{float(v)!r}" for d, v in grid.dropna().itertuples(index=False)]
            parsed, source = tracker.parse_manual_prices("\n".join(lines), decimal=".", date_order="dmy"), "typed rows"
    except PriceParseError as exc:
        error = str(exc)

    if error:
        st.error(error)
    if parsed is not None:
        if parsed.ok:
            st.success(parsed.describe())
        else:
            st.error("No valid price rows were found.")
        for w in parsed.warnings:
            st.warning(w)
        if parsed.errors:
            with st.expander(f"{parsed.n_errors} line(s) cannot be used", expanded=not parsed.ok):
                for e in parsed.errors:
                    st.write("- " + e)
        if parsed.ok:
            rows = parsed.rows if len(parsed.rows) <= 12 else parsed.rows[:6] + parsed.rows[-6:]
            show_df(pd.DataFrame(rows, columns=["Date", "Price"]),
                    column_config={"Date": st.column_config.DateColumn(format="YYYY-MM-DD"),
                                   "Price": st.column_config.NumberColumn(format="%.4f")})
            if len(parsed.rows) > 12:
                st.caption(f"First and last 6 of {len(parsed.rows)} prices.")
            mode = st.radio("If this stock already has manual prices",
                            ["Add to them (a day in the new data overwrites that day)",
                             "Replace all manual prices of this stock"], key=f"mp_mode_{nonce}")
            skip_ok = True
            if parsed.n_errors:
                skip_ok = st.checkbox(f"Skip the {parsed.n_errors} unusable line(s) and save the rest",
                                      key=f"mp_skip_{nonce}")
            if st.button("💾  Save these prices", key="mp_save", type="primary", disabled=not skip_ok):
                n = tracker.save_manual_prices(key, parsed.rows, source=source, replace=mode.startswith("Replace"))
                flash("success", f"{n} manual price(s) saved for {label}. They are used immediately.")
                if s.get("export_excel"):
                    post_outcome(tracker.export(p.account), "Manual prices")
                st.session_state["mp_nonce"] = nonce + 1
                st.rerun()

    if existing:
        with st.expander(f"Saved manual prices of {label} ({len(existing)})"):
            ex = pd.DataFrame(existing, columns=["Date", "Price"])
            c1, c2 = st.columns([1, 1])
            with c1:
                show_df(ex, column_config={"Date": st.column_config.DateColumn(format="YYYY-MM-DD"),
                                           "Price": st.column_config.NumberColumn(format="%.4f")}, height=260)
            with c2:
                if len(ex) >= 2:
                    st.line_chart(ex.assign(Date=pd.to_datetime(ex["Date"])).set_index("Date"), height=260,
                                  **stretch())
            st.download_button("⬇  Download as CSV", data=prices_to_text(existing).encode("utf-8"),
                               file_name=f"manual_prices_{key.replace('|', '_').replace('@', '')}.csv",
                               mime="text/csv", key="mp_download")
            sure = st.checkbox("Yes, delete all manual prices of this stock", key=f"mp_sure_{nonce}")
            if st.button("🗑  Delete", key="mp_delete", disabled=not sure):
                n = tracker.delete_manual_prices(key)
                flash("info", f"{n} manual price(s) of {label} deleted.")
                if s.get("export_excel"):
                    post_outcome(tracker.export(p.account), "Manual prices")
                st.session_state["mp_nonce"] = nonce + 1
                st.rerun()


# ---- tab 4: coverage ---------------------------------------------------------------------------------------------
def price_coverage(snap: Snapshot) -> None:
    st.markdown("#### Price coverage of the performance history")
    h = snap.history()
    if h is None or h.empty:
        st.info("The history needs the numbers - see the notes above.")
        return
    st.caption("Every day a stock was held is valued with its newest known close. This table shows where the prices "
               "come from and where they run out - add what is missing under *Manual price history*.")
    for note in h.notes:
        st.warning(note)
    cov = T.coverage_table(h)
    if len(cov):
        show_df(cov, column_config=COVERAGE_COLUMNS)
    if not h.notes:
        st.success("Every held day is valued with a real, recent price.")


def archive(tracker: Tracker, account: str) -> None:
    st.markdown("#### Imported statements" + (" (all accounts)" if account == CONSOLIDATED else ""))
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
        c[2].write(f"{r.period_start} → {r.period_end}" + (f"  ·  {r.account}" if account == CONSOLIDATED else ""))
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
    st.markdown("#### Excel workbook" + (" - all accounts merged" if p.is_consolidated else ""))
    if p.is_consolidated:
        st.caption(f"One workbook for the unified portfolio of {', '.join(p.account_ids)}: combined positions, cash, "
                   "performance and transaction histories (Sections 1-4 carry an Account column).")
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
    if hide_closed_dust(s):
        st.caption("The view filter is **on** (sidebar): closed positions and cash balances below 0.5 are left out of "
                   "Sections 6 and 7 of the workbook and of positions.csv / cash.csv. Totals, returns and CAGR still "
                   "include them; transaction history and the Checks sheet stay complete.")
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

    st.markdown("#### CSV files")
    if snap.metrics is None:
        st.info("The CSV export needs the numbers - see the Market data tab.")
        return
    style_options = list(CSV_STYLES)
    style = st.radio("CSV format", style_options, index=style_options.index(csv_style(s)), horizontal=True,
                     format_func=CSV_STYLE_LABELS.get, key="w_csvformat",
                     help="Excel under a Hungarian / German / French ... regional setting needs semicolons and a "
                          "decimal comma to open a CSV file with a double click; everything else (pandas, R, Google "
                          "Sheets) expects the international form.")
    if style != s.get("csv_format"):
        tracker.update_settings(csv_format=style)
    files = tracker.csv_files(snap, style)
    c1, c2 = st.columns(2)
    c1.download_button("⬇  Download all tables (.zip of CSV files)", data=tracker.csv_zip(snap, style),
                       file_name=f"IBKR_Portfolio_{p.account}_csv.zip", mime="application/zip", key="download_csv_zip",
                       **stretch())
    if c2.button("💾  Save the CSV files to the output folder", key="save_csv", **stretch()):
        written = tracker.write_csv(snap, style)
        flash("success", f"{len(written)} CSV files written to {written[0].parent}")
        st.rerun()
    with st.expander("Single tables"):
        for name, data in files.items():
            st.download_button(f"⬇  {name}", data=data, file_name=f"IBKR_Portfolio_{p.account}_{name}",
                               mime="text/csv", key=f"download_csv_{name}")
    st.caption("UTF-8 with byte order mark (opens correctly in Excel). " +
               ("Record tables start with an Account column; accounts.csv is the per-account breakdown."
                if p.is_consolidated else "positions.csv has the statement share count and the transaction sum."))


# ======================================================================================================
# page
# ======================================================================================================
def main() -> None:
    st.set_page_config(page_title="IBKR Portfolio Tracker", page_icon="📈", layout="wide",
                       initial_sidebar_state="expanded")
    st.markdown("<style>.block-container{padding-top:2.2rem;} div[data-testid='stMetricValue']{font-size:1.55rem;}"
                ".sec-label{font-size:.78rem;opacity:.72;line-height:1.25;} .sec-value{font-size:1.05rem;font-weight:600;}"
                "</style>", unsafe_allow_html=True)
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
    head = (f"{CONSOLIDATED_LABEL}: {', '.join(p.account_ids)}" if p.is_consolidated
            else f"{p.account}{(' · ' + p.alias) if p.alias else ''}")
    st.caption(f"{head} · base currency {p.base_currency} · "
               f"statements {p.period_start} → {p.period_end} · {len(p.statements)} file(s) merged")
    if snap.problem:
        st.error(snap.problem)

    tabs = st.tabs(["Dashboard", "Transactions", "Reconciliation", "Market data", "Archive", "Excel export"])
    with tabs[0]:
        if snap.metrics is not None:
            dashboard(snap, tracker)
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
