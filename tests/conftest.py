"""Shared fixtures.  The sample statements of the task live in ``examples/statements`` and double as the
primary regression data; ``synth.py`` provides a second, ground-truth account for everything the sample lacks."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fakes import FakeProvider                                  # noqa: E402
from synth import make_csv                                      # noqa: E402
from ibkr_tracker.config import DEFAULT_SETTINGS                # noqa: E402
from ibkr_tracker.excel.recalc import find_soffice              # noqa: E402
from ibkr_tracker.market import MarketDataService               # noqa: E402
from ibkr_tracker.parsing import parse_statement, parse_statement_file   # noqa: E402
from ibkr_tracker.portfolio import merge_statements             # noqa: E402
from ibkr_tracker.storage import Store                          # noqa: E402
from ibkr_tracker.tracker import Tracker                        # noqa: E402

SAMPLES = ROOT / "examples" / "statements"
SAMPLE_CSV = SAMPLES / "U24013787_20260101_20261023.csv"
SAMPLE_HTML = SAMPLES / "U24013787_20260101_20261023.htm"
TODAY = date(2026, 10, 30)                                      # "today" of the fake Yahoo (after the statement end)

needs_lo = pytest.mark.skipif(find_soffice() is None, reason="LibreOffice is not installed")


def pytest_configure(config):
    config.addinivalue_line("markers", "gui: browser / Streamlit tests")


# ---- parsed statements (session scoped: parsing the HTML takes a moment) --------------------------------------
@pytest.fixture(scope="session")
def stmt_csv():
    return parse_statement_file(SAMPLE_CSV)


@pytest.fixture(scope="session")
def stmt_html():
    return parse_statement_file(SAMPLE_HTML)


@pytest.fixture(scope="session")
def pf_csv(stmt_csv):
    return merge_statements([stmt_csv])


@pytest.fixture(scope="session")
def pf_html(stmt_html):
    return merge_statements([stmt_html])


@pytest.fixture(scope="session")
def pf_both(stmt_csv, stmt_html):
    return merge_statements([stmt_csv, stmt_html])


@pytest.fixture(scope="session")
def synth():
    data, truth = make_csv()
    return parse_statement("synthetic.csv", data), truth


@pytest.fixture(scope="session")
def pf_synth(synth):
    return merge_statements([synth[0]])


# ---- settings / store / tracker ---------------------------------------------------------------------------------
@pytest.fixture
def offline():
    return dict(DEFAULT_SETTINGS, price_source="statement", recalc_with_libreoffice=False)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "tracker.db")


@pytest.fixture
def service(store):
    return MarketDataService(store)


@pytest.fixture(autouse=True)
def _never_touch_the_real_data_folder(tmp_path_factory, monkeypatch):
    """Safety net: whatever a test does, ``./data`` of the project stays untouched."""
    monkeypatch.setenv("IBKR_TRACKER_HOME", str(tmp_path_factory.mktemp("default_home")))


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    monkeypatch.setenv("IBKR_TRACKER_HOME", str(path))
    return path


@pytest.fixture
def fake():
    return FakeProvider(last_day=TODAY)


@pytest.fixture
def tracker(home, fake):
    """A tracker whose 'Yahoo' is the deterministic fake; LibreOffice embedding is switched off for speed."""
    t = Tracker(Store(home / "tracker.db"), fake, today=lambda: TODAY)
    t.update_settings(recalc_with_libreoffice=False)
    return t


@pytest.fixture
def loaded(tracker):
    """Tracker with the two sample statements imported."""
    for f in (SAMPLE_CSV, SAMPLE_HTML):
        res = tracker.import_statement(f.name, f.read_bytes())
        assert res.ok, res.message
    return tracker
