#!/usr/bin/env bash
# IBKR Portfolio Tracker - start the app (macOS / Linux).
# First run: creates a private virtual environment in ./.venv and installs requirements.txt.
# Optional: PYTHON=python3.12 ./run_app.sh      IBKR_TRACKER_PORT=8600 ./run_app.sh      IBKR_TRACKER_HOME=/some/folder ./run_app.sh
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
PORT="${IBKR_TRACKER_PORT:-8501}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "Python 3.10+ was not found. Install it from https://www.python.org/downloads/ and run this script again." >&2
    exit 1
fi
if [ ! -x .venv/bin/python ]; then
    echo "Creating the virtual environment (.venv) - first start only ..."
    "$PY" -m venv .venv
fi
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt \
    || echo "Warning: could not install/verify the requirements (offline?) - trying to start anyway."
echo "Starting IBKR Portfolio Tracker on http://localhost:${PORT}  (Ctrl+C to stop)"
.venv/bin/python scripts/open_when_ready.py "$PORT" >/dev/null 2>&1 &
exec .venv/bin/python -m streamlit run app.py --server.port "$PORT" "$@"
