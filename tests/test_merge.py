"""Merging of several statements: overlap, duplicates, multiplicity, gaps, accounts."""
from __future__ import annotations

from datetime import date

import pytest

from synth import make_csv
from ibkr_tracker.parsing import parse_statement
from ibkr_tracker.portfolio import merge_multiset, merge_statements


def _stmt(name, **kw):
    data, truth = make_csv(**kw)
    return parse_statement(name, data), truth


def test_csv_plus_html_of_the_same_statement_is_counted_once(pf_csv, pf_both, stmt_csv, stmt_html):
    assert len(pf_both.trades) == len(pf_csv.trades) == 22
    assert len(pf_both.fx_trades) == len(pf_csv.fx_trades) == 62
    assert len(pf_both.cash_flows) == len(pf_csv.cash_flows) == 14
    assert len(pf_both.dividends) == len(pf_csv.dividends) == 10
    assert pf_both.duplicates_dropped > 0
    # the full-precision (CSV) record wins over the rounded HTML one
    assert all(t.precision == "full" for t in pf_both.trades)
    assert pf_both.latest.fmt == "csv"                           # reconciliation reference = the precise statement
    assert [t.fee for t in pf_both.trades] == [t.fee for t in pf_csv.trades]


def test_order_of_upload_does_not_matter(stmt_csv, stmt_html, pf_both):
    swapped = merge_statements([stmt_html, stmt_csv])
    assert swapped.signature() == pf_both.signature()
    assert swapped.cash_balances() == pf_both.cash_balances()


def test_overlapping_periods_do_not_double_count():
    full, truth = _stmt("full.csv")
    first_half, _ = _stmt("h1.csv", period_end=date(2025, 6, 30), only_before=date(2025, 7, 1))
    second_half, _ = _stmt("full2.csv")                          # full year again, overlapping the first half
    merged = merge_statements([first_half, second_half])
    alone = merge_statements([full])
    assert merged.duplicates_dropped > 0
    assert len(merged.trades) == len(alone.trades) == 3
    assert len(merged.cash_flows) == len(alone.cash_flows) == 5
    assert len(merged.dividends) == len(alone.dividends) == 3
    assert merged.cash_balances()["EUR"] == pytest.approx(truth.cash["EUR"])
    assert merged.cash_balances()["USD"] == pytest.approx(truth.cash["USD"])
    assert merged.warnings == []


def test_same_file_twice_is_idempotent():
    a, truth = _stmt("a.csv")
    b, _ = _stmt("b.csv")
    merged = merge_statements([a, b])
    assert len(merged.trades) == 3 and merged.cash_balances()["EUR"] == pytest.approx(truth.cash["EUR"])


def test_two_identical_fills_inside_one_statement_stay_two():
    """Multiplicity aware: only a record repeated *across statements* is a duplicate."""
    data, _ = make_csv()
    text = data.decode()
    line = next(l for l in text.splitlines() if l.startswith("Trades,Data,Order,Stocks,USD,AAPL") and ",10.0," in l)
    doubled = text.replace(line, line + "\n" + line)
    one = parse_statement("one.csv", data)
    two = parse_statement("two.csv", doubled.encode())
    assert len(two.trades) == 4
    assert len(merge_statements([two]).trades) == 4
    both = merge_statements([one, two])
    assert len(both.trades) == 4                                  # the first file contributes 1 of the 2 fills
    assert both.duplicates_dropped >= 3


def test_consecutive_statements_are_concatenated_chronologically():
    full_truth = make_csv()[1]
    h1, _ = _stmt("h1.csv", period_end=date(2025, 6, 30), only_before=date(2025, 7, 1))
    h2, _ = _stmt("h2.csv", period_start=date(2025, 7, 1), only_from=date(2025, 7, 1))
    assert len(h1.trades) == 3 and len(h2.trades) == 0 and len(h2.cash_flows) == 1 and len(h2.div_lines) == 3
    merged = merge_statements([h2, h1])                           # upload order does not matter
    assert (merged.period_start, merged.period_end) == (date(2025, 1, 1), date(2025, 12, 31))
    assert len(merged.cash_flows) == 5 and len(merged.dividends) == 3 and merged.duplicates_dropped == 0
    assert [c.dt for c in merged.cash_flows] == sorted(c.dt for c in merged.cash_flows)
    assert merged.cash_balances()["EUR"] == pytest.approx(full_truth.cash["EUR"])
    assert merged.cash_balances()["USD"] == pytest.approx(full_truth.cash["USD"])
    assert merged.warnings == []
    assert merged.holdings == full_truth.holdings                  # h2 (the newest statement) still shows the positions
    assert merged.holdings_ref.startswith("h2.csv")


def test_gap_between_statements_is_reported():
    early, _ = _stmt("early.csv", period_end=date(2025, 3, 31), only_before=date(2025, 4, 1))
    late, _ = _stmt("late.csv", period_start=date(2025, 7, 1))
    merged = merge_statements([early, late])
    assert any("Gap in statement coverage" in w for w in merged.warnings)


def test_non_zero_starting_cash_is_reported():
    data, _ = make_csv(period_start=date(2025, 3, 1))
    text = data.decode().replace("Starting Cash,EUR,0,0,0", "Starting Cash,EUR,500,500,0", 1)
    ps = parse_statement("later.csv", text.encode())
    merged = merge_statements([ps])
    assert any("starts with non-zero cash" in w for w in merged.warnings)


def test_other_accounts_are_never_mixed():
    a, _ = _stmt("a.csv")
    data, _ = make_csv()
    other = parse_statement("other.csv", data.decode().replace("U9990001", "U1111111").encode())
    merged = merge_statements([a, other], account="U9990001")
    assert merged.account == "U9990001" and len(merged.trades) == 3
    assert any("other accounts" in w for w in merged.warnings)
    only_other = merge_statements([a, other], account="U1111111")
    assert only_other.account == "U1111111" and len(only_other.trades) == 3


def test_empty_merge_is_an_error():
    with pytest.raises(ValueError):
        merge_statements([])


def test_merge_multiset_prefers_full_precision():
    class R:
        def __init__(self, v, precision):
            self.v, self.precision = v, precision

    full, rounded = R(1.2345, "full"), R(1.23, "rounded")
    out, dropped = merge_multiset([[rounded], [full]], key_fn=lambda r: "k", amount_fn=lambda r: r.v)
    assert dropped == 1 and out == [full]
    out, dropped = merge_multiset([[full], [rounded]], key_fn=lambda r: "k", amount_fn=lambda r: r.v)
    assert dropped == 1 and out == [full]
    out, dropped = merge_multiset([[R(1.0, "full")], [R(9.0, "full")]], key_fn=lambda r: "k", amount_fn=lambda r: r.v)
    assert dropped == 0 and len(out) == 2                          # same key but different amounts: two records


def test_signature_changes_with_content(pf_csv, pf_both, pf_synth):
    assert len({pf_csv.signature(), pf_both.signature(), pf_synth.signature()}) == 3
    assert pf_csv.fingerprint() == pf_both.fingerprint()           # same transactions -> same fingerprint
