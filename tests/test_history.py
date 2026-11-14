"""Day-by-day reconstruction of the whole lifespan (``ibkr_tracker.history``): hand-computed truth.

The synthetic account (``synth.py``) is replayed with hand-made price and FX series (``mdfactory.make_md``), so
every expected number below comes from the ledger written out in this file - not from the application."""
from __future__ import annotations

import math
from datetime import date, timedelta

import pandas as pd
import pytest

from conftest import SAMPLE_CSV, TODAY
from fakes import FakeProvider
from mdfactory import D0, END, bench_px, daily, fx_eur, make_md, market, n_of, px_aapl, px_sap, px_tsla
from synth import ACCOUNT, USD_ACCOUNT, make_csv, make_usd_csv
from ibkr_tracker import tables as T
from ibkr_tracker.calc import compute_metrics
from ibkr_tracker.history import MIN_CAGR_YEARS, STALE_DAYS, build_history
from ibkr_tracker.parsing import parse_statement
from ibkr_tracker.portfolio import consolidate, merge_statements
from ibkr_tracker.storage import Store
from ibkr_tracker.tracker import Tracker

FIRST = date(2025, 1, 10)                                 # the first transaction of the synthetic account


# ---- the ledger of the synthetic account, written out by hand ---------------------------------------------------------
CASH_EVENTS = [                                           # (day, currency, amount) - see synth.py
    (date(2025, 1, 10), "EUR", 10000.0),                  # deposit
    (date(2025, 1, 13), "EUR", -3000.0 - 2.0),            # forex: sell 3000 EUR, commission 2.0 EUR ...
    (date(2025, 1, 13), "USD", 3000.0 * 1.03),            # ... for 3090 USD
    (date(2025, 1, 14), "USD", -1500.0 - 1.0),            # AAPL buy 10 @ 150
    (date(2025, 1, 15), "EUR", -600.0 - 1.0),             # SAP buy 5 @ 120
    (date(2025, 2, 3), "EUR", -1.5),                      # market data fee
    (date(2025, 2, 13), "USD", 2.50 - 0.38),              # AAPL dividend net of tax
    (date(2025, 3, 4), "EUR", 0.12),                      # interest
    (date(2025, 3, 5), "EUR", 5000.0),                    # deposit
    (date(2025, 5, 20), "EUR", 11.75 - 3.10),             # SAP dividend net of tax
    (date(2025, 6, 10), "USD", 680.0 - 1.0),              # AAPL sell 4 @ 170
    (date(2025, 8, 14), "USD", 1.50 - 0.23 - 0.05),       # AAPL dividend net of tax and fee
    (date(2025, 9, 1), "EUR", -2000.0),                   # withdrawal
]
CONTRIBUTIONS = [(date(2025, 1, 10), 10000.0), (date(2025, 2, 3), -1.5), (date(2025, 3, 5), 5000.0),
                 (date(2025, 9, 1), -2000.0)]             # EUR; the interest is not a contribution
STOCK_TRADES = [(date(2025, 1, 14), "AAPL", 10.0, 150.0, "USD"), (date(2025, 1, 15), "SAP", 5.0, 120.0, "EUR"),
                (date(2025, 6, 10), "AAPL", -4.0, 170.0, "USD")]


def shares(d: date) -> dict[str, float]:
    out = {"AAPL": 0.0, "SAP": 0.0}
    for day, sym, qty, _price, _ccy in STOCK_TRADES:
        if day <= d:
            out[sym] += qty
    return out


def oracle(d: date) -> dict[str, float]:
    eur = sum(a for day, c, a in CASH_EVENTS if day <= d and c == "EUR")
    usd = sum(a for day, c, a in CASH_EVENTS if day <= d and c == "USD")
    q = shares(d)
    stocks = q["AAPL"] * px_aapl(d) + q["SAP"] * px_sap(d) / fx_eur(d)
    cash = usd + eur / fx_eur(d)
    deposits = sum(a / fx_eur(day) for day, a in CONTRIBUTIONS if day <= d)
    return dict(stocks=stocks, cash=cash, total=stocks + cash, deposits=deposits,
                deposits_eur=sum(a for day, a in CONTRIBUTIONS if day <= d))


@pytest.fixture(scope="module")
def hist(pf_synth):
    md = market(pf_synth)
    m = compute_metrics(pf_synth, md)
    return build_history(pf_synth, md, m), m, md


def row(h, d: date):
    return h.frame.loc[pd.Timestamp(d)]


# ======================================================================================================
# the lifespan
# ======================================================================================================
def test_the_oracle_reproduces_the_statement_ledger(synth):
    _, truth = synth
    assert sum(a for _, c, a in CASH_EVENTS if c == "EUR") == pytest.approx(truth.cash["EUR"])
    assert sum(a for _, c, a in CASH_EVENTS if c == "USD") == pytest.approx(truth.cash["USD"])


def test_one_row_per_calendar_day_from_the_first_transaction_to_the_valuation_date(hist):
    h, _, md = hist
    assert h.start == FIRST == date(2025, 1, 10) and h.end == END == md.as_of
    assert h.days == (END - FIRST).days + 1
    assert list(h.frame.index) == [pd.Timestamp(FIRST + timedelta(days=i)) for i in range(h.days)]
    assert h.frame.index.name == "Date" and not h.empty


def test_every_day_matches_the_hand_computed_ledger(hist):
    h, _, _ = hist
    d = FIRST
    while d <= END:                                        # all 356 days, weekends included
        want, got = oracle(d), row(h, d)
        assert got["stocks_usd"] == pytest.approx(want["stocks"], abs=1e-6), d
        assert got["cash_usd"] == pytest.approx(want["cash"], abs=1e-6), d
        assert got["total_usd"] == pytest.approx(want["total"], abs=1e-6), d
        assert got["deposits_usd"] == pytest.approx(want["deposits"], abs=1e-6), d
        assert got["deposits_base"] == pytest.approx(want["deposits_eur"], abs=1e-9), d
        assert got["total_base"] == pytest.approx(want["total"] * fx_eur(d), abs=1e-6), d
        d += timedelta(days=1)


def test_the_last_day_equals_the_headline_numbers(hist):
    h, m, _ = hist
    assert h.matches(m) == []
    last = h.last
    assert last["total_usd"] == pytest.approx(m.usd_total) and last["deposits_usd"] == pytest.approx(m.usd_deposits)
    assert last["return_usd"] == pytest.approx(m.usd_return) and last["cagr_usd"] == pytest.approx(m.usd_cagr)
    assert last["total_base"] == pytest.approx(m.base_total) and last["return_base"] == pytest.approx(m.base_return)
    assert last["cagr_base"] == pytest.approx(m.base_cagr) and last["years"] == pytest.approx(m.years)


def test_deposits_step_on_their_own_day_at_the_rate_of_that_day(hist):
    h, _, _ = hist
    dep = h.frame["deposits_usd"]
    steps = dep.diff().fillna(dep.iloc[0])
    changed = {ts.date(): v for ts, v in steps[steps.abs() > 1e-9].items()}
    assert set(changed) == {day for day, _ in CONTRIBUTIONS}
    for day, eur in CONTRIBUTIONS:
        assert changed[day] == pytest.approx(eur / fx_eur(day))
    assert date(2025, 3, 4) not in changed                 # the interest is not a contribution


def test_weekends_are_carried_forward_from_the_friday_close(hist):
    h, _, _ = hist
    fri, sat, sun = date(2025, 7, 4), date(2025, 7, 5), date(2025, 7, 6)
    assert row(h, fri)["stocks_usd"] == pytest.approx(row(h, sat)["stocks_usd"]) == pytest.approx(row(h, sun)["stocks_usd"])
    assert row(h, sat)["total_usd"] == pytest.approx(oracle(sat)["total"])


def test_return_and_cagr_by_hand_on_a_mid_year_day(hist):
    h, _, _ = hist
    d = date(2025, 6, 30)
    contrib = [(day, a / fx_eur(day)) for day, a in CONTRIBUTIONS if day <= d]
    total = sum(u for _, u in contrib)
    years = sum(u * (d - day).days for day, u in contrib) / total / 365
    ret = oracle(d)["total"] / total
    got = row(h, d)
    assert got["years"] == pytest.approx(years) and got["return_usd"] == pytest.approx(ret)
    assert got["cagr_usd"] == pytest.approx(ret ** (1 / years) - 1)
    assert got["gain_usd"] == pytest.approx(oracle(d)["total"] - total)


def test_benchmark_replay_by_hand(hist):
    h, m, _ = hist
    assert h.benchmark_available
    d = date(2025, 7, 1)
    inv = units = 0.0
    for day, sym, qty, price, ccy in STOCK_TRADES:
        if day <= d:
            usd = qty * price / (fx_eur(day) if ccy == "EUR" else 1.0)
            inv += usd
            units += usd / bench_px(day)
    got = row(h, d)
    assert got["invested_usd"] == pytest.approx(inv) and got["bench_value_usd"] == pytest.approx(units * bench_px(d))
    assert got["bench_excess_usd"] == pytest.approx(got["stocks_usd"] - units * bench_px(d))
    assert got["ret_bench"] == pytest.approx(units * bench_px(d) / inv)
    b = m.benchmark                                        # ... and the last day is the point-in-time simulation
    last = h.last
    assert last["bench_value_usd"] == pytest.approx(b.value_usd) and last["bench_excess_usd"] == pytest.approx(b.excess_usd)
    assert last["cagr_bench"] == pytest.approx(b.cagr_vuaa) and last["cagr_stocks"] == pytest.approx(b.cagr_stocks)
    assert last["bench_years"] == pytest.approx(b.years)


def test_positions_frame_values_every_stock_in_usd(hist):
    h, m, _ = hist
    assert list(h.positions.columns) == ["AAPL", "SAP"]
    d = date(2025, 3, 3)
    assert h.positions.loc[pd.Timestamp(d), "AAPL"] == pytest.approx(10 * px_aapl(d))
    assert h.positions.loc[pd.Timestamp(d), "SAP"] == pytest.approx(5 * px_sap(d) / fx_eur(d))
    assert h.positions.sum(axis=1).to_numpy() == pytest.approx(h.frame["stocks_usd"].to_numpy())
    assert h.frame["n_positions"].loc[pd.Timestamp(date(2025, 1, 14))] == 1      # only AAPL on the 14th
    assert h.frame["n_positions"].loc[pd.Timestamp(date(2025, 1, 15))] == 2


# ======================================================================================================
# shares: splits, shares nobody bought, positions sold before the archive
# ======================================================================================================
def test_a_split_is_applied_like_the_open_positions_do():
    """AAPL splits 10:1 on 2025-11-01.  Trades stay pre-split, the Open Positions show 60 shares: every day is valued
    with today's shares x the split-adjusted close, so the value does not jump on the split day."""
    data, _ = make_csv(split=("AAPL", 10.0, date(2025, 11, 1)))
    pf = merge_statements([parse_statement("s.csv", data)])
    plain = merge_statements([parse_statement("p.csv", make_csv()[0])])
    adj = {"AAPL|USD": daily(D0, END, lambda d: px_aapl(d) / 10, True)}
    h = build_history(pf, market(pf, prices=adj))
    ref = build_history(plain, market(plain))
    for d in (date(2025, 1, 20), date(2025, 6, 9), date(2025, 6, 10), date(2025, 10, 31), date(2025, 11, 1)):
        assert row(h, d)["stocks_usd"] == pytest.approx(row(ref, d)["stocks_usd"], rel=1e-12), d
    cov = h.coverage_for("AAPL", "USD")
    assert cov.share_factor == pytest.approx(10.0) and cov.opening_shares == 0.0
    assert h.matches(compute_metrics(pf, market(pf, prices=adj))) == []


def test_shares_no_transaction_explains_are_assumed_held_from_the_first_day():
    """Only the transactions from March are imported: the 10 AAPL and 5 SAP bought before are in the Open Positions
    of the statement and nowhere else - they are the opening balance."""
    data, _ = make_csv(only_from=date(2025, 3, 1))
    pf = merge_statements([parse_statement("s.csv", data)])
    md = market(pf)
    h = build_history(pf, md)
    first = h.start
    assert first == date(2025, 3, 4)
    q0 = row(h, first)["stocks_usd"]
    assert q0 == pytest.approx(10 * px_aapl(first) + 5 * px_sap(first) / fx_eur(first))
    after = date(2025, 6, 10)
    assert row(h, after)["stocks_usd"] == pytest.approx(6 * px_aapl(after) + 5 * px_sap(after) / fx_eur(after))
    assert h.coverage_for("AAPL", "USD").opening_shares == pytest.approx(10.0)
    assert any("no transaction in the imported statements" in n for n in h.notes)
    assert h.matches(compute_metrics(pf, md)) == []


def test_a_position_sold_before_the_archive_ends_is_held_until_the_sale():
    data, _ = make_csv(closed_position=True, only_from=date(2025, 3, 1))     # TSLA bought 02-20 (not imported), sold 05-06
    pf = merge_statements([parse_statement("s.csv", data)])
    h = build_history(pf, market(pf))
    d = date(2025, 4, 1)
    assert h.positions.loc[pd.Timestamp(d), "TSLA"] == pytest.approx(2 * px_tsla(d))
    assert h.positions.loc[pd.Timestamp(date(2025, 5, 6)), "TSLA"] == 0.0
    assert h.positions.loc[pd.Timestamp(date(2025, 5, 5)), "TSLA"] == pytest.approx(2 * px_tsla(date(2025, 5, 5)))


def test_a_position_that_was_opened_and_closed_inside_the_archive():
    data, _ = make_csv(closed_position=True)
    pf = merge_statements([parse_statement("s.csv", data)])
    h = build_history(pf, market(pf))
    t = h.positions["TSLA"]
    assert t.loc[pd.Timestamp(date(2025, 2, 19))] == 0.0
    assert t.loc[pd.Timestamp(date(2025, 2, 20))] == pytest.approx(2 * px_tsla(date(2025, 2, 20)))
    assert t.loc[pd.Timestamp(date(2025, 5, 6))] == 0.0 == t.iloc[-1]
    assert h.coverage_for("TSLA", "USD").held_days == (date(2025, 5, 5) - date(2025, 2, 20)).days + 1


# ======================================================================================================
# missing data
# ======================================================================================================
def test_a_stock_without_any_price_is_valued_at_its_last_trade_price(pf_synth):
    md = market(pf_synth, prices={"SAP|EUR": []})
    md.price_series.pop("SAP|EUR", None)
    m = compute_metrics(pf_synth, md)
    h = build_history(pf_synth, md, m)
    d = date(2025, 4, 2)
    assert h.positions.loc[pd.Timestamp(d), "SAP"] == pytest.approx(5 * 120.0 / fx_eur(d))      # the purchase price
    cov = h.coverage_for("SAP", "EUR")
    assert cov.fallback_days == cov.held_days and cov.points == 0 and not cov.complete
    assert any("SAP (EUR)" in n and "last purchase / sale price" in n for n in h.notes)
    assert h.matches(m), "the KPI values an unpriced stock at zero, the history at the trade price - reported"
    assert h.coverage_for("AAPL", "USD").complete


def test_a_price_that_ends_early_is_carried_forward_and_reported(pf_synth):
    ends = date(2025, 3, 31)
    md = market(pf_synth, prices={"SAP|EUR": daily(D0, ends, px_sap, True)})
    h = build_history(pf_synth, md)
    d = date(2025, 9, 15)
    assert h.positions.loc[pd.Timestamp(d), "SAP"] == pytest.approx(5 * px_sap(ends) / fx_eur(d))
    cov = h.coverage_for("SAP", "EUR")
    first_stale = ends + timedelta(days=STALE_DAYS + 1)
    assert cov.stale_days == (END - timedelta(days=1) - first_stale).days + 1       # the last day uses the current price
    assert any("older than" in n and "SAP (EUR)" in n for n in h.notes)
    assert h.matches(compute_metrics(pf_synth, md)) == []                          # the current price IS the last close


def test_manual_prices_fill_the_gap_and_win_over_downloaded_ones(pf_synth):
    yahoo = daily(date(2025, 4, 1), END, px_sap, True)                      # Yahoo only knows SAP from April
    without = build_history(pf_synth, market(pf_synth, prices={"SAP|EUR": yahoo}))
    gap = (date(2025, 3, 31) - date(2025, 1, 15)).days + 1
    assert without.coverage_for("SAP", "EUR").fallback_days == gap
    manual = daily(date(2025, 1, 15), date(2025, 3, 31), lambda d: 500.0, True) + [(date(2025, 6, 2), 999.0)]
    md = market(pf_synth, prices={"SAP|EUR": yahoo}, manual={"SAP|EUR": manual})
    h = build_history(pf_synth, md)
    cov = h.coverage_for("SAP", "EUR")
    assert cov.fallback_days == 0 and cov.manual_points == len(manual) and cov.complete
    assert h.positions.loc[pd.Timestamp(date(2025, 2, 11)), "SAP"] == pytest.approx(5 * 500.0 / fx_eur(date(2025, 2, 11)))
    d = date(2025, 6, 2)                                                    # a day both have: the manual price wins
    assert h.positions.loc[pd.Timestamp(d), "SAP"] == pytest.approx(5 * 999.0 / fx_eur(d))
    d = date(2025, 6, 3)
    assert h.positions.loc[pd.Timestamp(d), "SAP"] == pytest.approx(5 * px_sap(d) / fx_eur(d))
    assert not any("SAP (EUR)" in n for n in h.notes)                       # no coverage complaint about SAP any more


def test_a_missing_fx_series_is_reported_and_not_fatal():
    data, _ = make_csv(dust_currency=True)                                  # 0.45 GBP are left on the account
    pf = merge_statements([parse_statement("s.csv", data)])
    md = market(pf)                                                          # no GBP rates at all
    m = compute_metrics(pf, md)
    h = build_history(pf, md, m)
    assert any("No FX rates for GBP" in n for n in h.notes)
    assert h.matches(m) == []                                               # the KPI leaves GBP out, too


def test_without_benchmark_prices_the_curve_is_left_out(pf_synth):
    h = build_history(pf_synth, market(pf_synth, bench=None))
    assert not h.benchmark_available and not h.benchmark_priced and h.frame["bench_value_usd"].isna().all()
    assert any("No benchmark prices" in n for n in h.notes)
    assert list(T.history_chart_frame(h, "benchmark").columns) == ["Stocks (USD)"]


def test_an_account_with_open_positions_but_no_transactions():
    """Everything came from the Open Positions: the lifespan starts with the statement period, the shares are the
    opening balance, there are no deposits (so no return) and nothing to replay into the benchmark."""
    data, _ = make_csv(only_from=date(2026, 1, 1))                          # no transaction survives the filter
    pf = merge_statements([parse_statement("s.csv", data)])
    assert pf.first_activity() == date(2025, 1, 1)                          # the statement period starts there
    h = build_history(pf, market(pf))
    assert h.start == date(2025, 1, 1) and h.end == END and h.days == 365
    d = date(2025, 6, 2)
    assert row(h, d)["stocks_usd"] == pytest.approx(6 * px_aapl(d) + 5 * px_sap(d) / fx_eur(d))
    assert (h.frame["deposits_usd"] == 0).all() and h.frame["return_usd"].isna().all()
    assert h.frame["cagr_usd"].isna().all()
    assert h.benchmark_priced and not h.benchmark_available
    assert any("nothing to replay" in n for n in h.notes) and not any("No benchmark prices" in n for n in h.notes)
    assert h.matches(compute_metrics(pf, market(pf))) == []


# ======================================================================================================
# several accounts
# ======================================================================================================
def _generic_market(pf):
    """Price series for every stock of ``pf`` (a different level per ticker), the EUR rate and the benchmark."""
    prices = {f"{t}|{c}": daily(D0, END, lambda d, k=sum(map(ord, t)): 50 + k % 70 + 0.1 * n_of(d), True)
              for t, c in pf.positions_keys()}
    return make_md(pf, prices=prices, fx={"EUR": daily(D0, END, fx_eur, True)}, bench=daily(D0, END, bench_px, True),
                   as_of=END)


def test_consolidated_history_is_the_sum_of_the_accounts():
    eur = merge_statements([parse_statement("e.csv", make_csv()[0])])
    usd_data, _ = make_usd_csv()
    usd = merge_statements([parse_statement("u.csv", usd_data)], USD_ACCOUNT)
    cons = consolidate([usd, eur])
    hc, he, hu = (build_history(p, _generic_market(p)) for p in (cons, eur, usd))
    assert hc.start == min(he.start, hu.start) and hc.end == END
    for col in ("stocks_usd", "cash_usd", "total_usd", "deposits_usd"):
        parts = he.frame[col].reindex(hc.frame.index, fill_value=0.0) + hu.frame[col].reindex(hc.frame.index, fill_value=0.0)
        assert hc.frame[col].to_numpy() == pytest.approx(parts.to_numpy(), abs=1e-6), col
    mc = compute_metrics(cons, _generic_market(cons))
    assert hc.matches(mc) == []


# ======================================================================================================
# independent of refresh timestamps
# ======================================================================================================
def test_history_covers_the_lifespan_without_a_single_refresh(home):
    t = Tracker(Store(home / "t.db"), FakeProvider(last_day=TODAY), today=lambda: TODAY)
    t.update_settings(export_excel=False, recalc_with_libreoffice=False)
    assert t.import_statement(SAMPLE_CSV.name, SAMPLE_CSV.read_bytes()).ok
    snap = t.snapshot("U24013787")
    assert t.history("U24013787") == []                                     # nothing was ever recorded at a refresh
    h = snap.history()
    assert h is not None and not h.empty and h.days > 250 and h.start == date(2026, 1, 14)
    assert h.matches(snap.metrics) == []
    t.refresh_prices("U24013787")
    snap2 = t.snapshot("U24013787")
    assert len(t.history("U24013787")) == 1                                # the old per-refresh record still works ...
    h2 = snap2.history()
    assert h2.start == h.start and h2.days >= h.days                       # ... but the chart no longer depends on it
    assert h2.matches(snap2.metrics) == []
    assert snap2.history() is h2                                           # computed once per snapshot


def test_the_view_filter_does_not_change_the_history(home):
    t = Tracker(Store(home / "t.db"), FakeProvider(last_day=TODAY), today=lambda: TODAY)
    t.update_settings(export_excel=False, recalc_with_libreoffice=False)
    t.import_statement(SAMPLE_CSV.name, SAMPLE_CSV.read_bytes())
    t.refresh_prices("U24013787")
    on = t.snapshot("U24013787", dict(t.settings(), hide_closed_dust=True)).history()
    off = t.snapshot("U24013787", dict(t.settings(), hide_closed_dust=False)).history()
    pd.testing.assert_frame_equal(on.frame, off.frame)


# ======================================================================================================
# tables for the charts and the exports
# ======================================================================================================
def test_history_table_has_unique_labels_and_hides_the_young_annualised_values(hist):
    h, _, _ = hist
    df = T.history_table(h)
    assert df.columns[0] == "Date" and df["Date"].iloc[0] == FIRST and df["Date"].iloc[-1] == END
    assert df.columns.is_unique and "Total value (EUR)" in df.columns and "Total value (USD)" in df.columns
    assert "AAPL (USD)" in df.columns and "SAP (USD)" in df.columns
    young = h.frame["years"] < MIN_CAGR_YEARS
    assert young.any() and (~young).any()
    assert df.loc[young.to_numpy(), "CAGR (USD)"].isna().all()
    assert df.loc[~young.to_numpy(), "CAGR (USD)"].notna().all()
    assert h.frame.loc[young, "cagr_usd"].notna().any()                    # the raw frame keeps the numbers


def test_a_usd_portfolio_has_no_duplicate_base_columns():
    data, _ = make_usd_csv()
    pf = merge_statements([parse_statement("u.csv", data)], USD_ACCOUNT)
    h = build_history(pf, _generic_market(pf))
    df = T.history_table(h)
    assert df.columns.is_unique and not any("(base)" in c for c in df.columns)
    assert "Total value (USD)" in df.columns and "USD per USD" not in " ".join(df.columns)
    assert T.history_columns(h) == [c for c in h.frame.columns if not c.endswith("_base") and c != "base_per_usd"]


def test_chart_frames(hist):
    h, _, _ = hist
    val = T.history_chart_frame(h, "value", "base")
    assert list(val.columns) == ["Portfolio value (EUR)", "Net deposits (EUR)"] and len(val) == h.days
    assert list(T.history_chart_frame(h, "value", "usd").columns) == ["Portfolio value (USD)", "Net deposits (USD)"]
    since = date(2025, 10, 1)
    assert T.history_chart_frame(h, "gain", "usd", since).index[0] == pd.Timestamp(since)
    cagr = T.history_chart_frame(h, "cagr", "usd")
    assert cagr.iloc[:30, 0].isna().all() and cagr.iloc[-1, 0] == pytest.approx(h.last["cagr_usd"])
    alloc = T.history_chart_frame(h, "allocation", "base")
    assert list(alloc.columns) == ["AAPL", "SAP", "Cash"]
    assert alloc.sum(axis=1).iloc[-1] == pytest.approx(h.last["total_base"])
    assert list(T.history_chart_frame(h, "benchmark", "usd").columns) == ["Stocks (USD)",
                                                                           "VUAA.L replay (USD)"]
    assert len(T.history_chart_frame(h, "return", "base")) == h.days
    with pytest.raises(ValueError):
        T.history_chart_frame(h, "nonsense")


def test_the_sample_account_history_is_complete(pf_csv, tmp_path):
    """The shipped sample with the deterministic fake provider: last day = KPI tiles, no coverage problems."""
    from ibkr_tracker.config import DEFAULT_SETTINGS
    from ibkr_tracker.market import MarketDataService
    store = Store(tmp_path / "s.db")
    svc = MarketDataService(store, FakeProvider(last_day=TODAY), today=lambda: TODAY)
    svc.refresh(pf_csv, DEFAULT_SETTINGS)
    md = svc.build(pf_csv, DEFAULT_SETTINGS)
    m = compute_metrics(pf_csv, md)
    h = build_history(pf_csv, md, m)
    assert h.matches(m) == [] and h.start == pf_csv.first_activity() and h.end == md.as_of
    assert all(c.complete for c in h.coverage) and h.notes == []
    assert not math.isnan(h.last["cagr_bench"]) and h.last["ret_bench"] == pytest.approx(m.benchmark.ret_vuaa)


# ======================================================================================================
# the notes
# ======================================================================================================
def test_a_first_import_gets_one_clear_note_instead_of_one_per_stock(pf_synth):
    """Nothing downloaded yet: only the period-end closes printed in the statement exist."""
    stmt = {"AAPL|USD": [(END, 190.0)], "SAP|EUR": [(END, 130.0)]}
    md = make_md(pf_synth, statement=stmt, fx={"EUR": daily(D0, END, fx_eur, True)}, as_of=END)
    h = build_history(pf_synth, md)
    assert h.notes[0].startswith("No daily prices have been downloaded yet")
    assert not any("Days without any price" in n for n in h.notes)
    assert len(h.notes) == 2 and "benchmark" in h.notes[1]                  # ... plus the missing benchmark
    d = date(2025, 4, 1)                                                    # before the statement date: the trade price
    assert h.positions.loc[pd.Timestamp(d), "AAPL"] == pytest.approx(10 * 150.0)


def test_stocks_with_the_same_kind_of_gap_share_one_note():
    data, _ = make_csv(closed_position=True)
    pf = merge_statements([parse_statement("s.csv", data)])
    md = market(pf, prices={"SAP|EUR": [], "TSLA|USD": []})
    md.price_series.pop("SAP|EUR", None), md.price_series.pop("TSLA|USD", None)
    h = build_history(pf, md)
    gaps = [n for n in h.notes if n.startswith("Days without any price")]
    assert len(gaps) == 1 and "SAP (EUR)" in gaps[0] and "TSLA (USD)" in gaps[0] and "AAPL" not in gaps[0]
    assert "no price data at all" in gaps[0] and "Manual price history" in gaps[0]


def test_the_coverage_table(pf_synth):
    ends = date(2025, 3, 31)
    md = market(pf_synth, prices={"SAP|EUR": daily(D0, ends, px_sap, True)}, manual={"AAPL|USD": [(date(2025, 2, 3), 151.0)]})
    h = build_history(pf_synth, md)
    df = T.coverage_table(h)
    assert list(df.columns) == ["Ticker", "Currency", "Status", "Days held", "Prices", "Yahoo", "Manual", "Statement",
                                "First price", "Last price"]
    aapl, sap = df.set_index("Ticker").loc["AAPL"], df.set_index("Ticker").loc["SAP"]
    assert aapl["Days held"] == (END - date(2025, 1, 14)).days + 1 and aapl["Manual"] == 1 and aapl["Yahoo"] == aapl["Prices"] - 1
    assert sap["Days held"] == (END - date(2025, 1, 15)).days + 1 and sap["Last price"] == ends.isoformat()
    assert sap["Status"] != "complete" and aapl["Status"] == "complete"


def test_the_coverage_table_shows_a_stock_without_any_price_with_blank_dates():
    data, _ = make_csv(closed_position=True)
    pf = merge_statements([parse_statement("s.csv", data)])
    md = market(pf, prices={"SAP|EUR": [], "TSLA|USD": []})
    md.price_series.pop("SAP|EUR", None), md.price_series.pop("TSLA|USD", None)
    df = T.coverage_table(build_history(pf, md)).set_index("Ticker")
    for ticker in ("SAP", "TSLA"):
        assert df.loc[ticker, "First price"] == "" and df.loc[ticker, "Last price"] == "" and df.loc[ticker, "Prices"] == 0
        assert df.loc[ticker, "Status"] != "complete"
    assert df.loc["AAPL", "First price"] != "" and df.loc["AAPL", "Status"] == "complete"
