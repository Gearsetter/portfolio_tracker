#!/usr/bin/env bash
# IBKR Portfolio Tracker - start the app (macOS / Linux).
# First run: creates a private virtual environment in ./.venv and installs requirements.txt.
# Optional: PYTHON=python3.12 ./run_app.sh      IBKR_TRACKER_PORT=8600 ./run_app.sh      IBKR_TRACKER_HOME=/some/folder ./run_app.sh
#           IBKR_TRACKER_VENV=/path/to/venv ./run_app.sh   (use that virtual environment instead of ./.venv - the dev container does this)
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
PORT="${IBKR_TRACKER_PORT:-8501}"
VENV="${IBKR_TRACKER_VENV:-.venv}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "Python 3.10+ was not found. Install it from https://www.python.org/downloads/ and run this script again." >&2
    exit 1
fi
if [ ! -x "$VENV/bin/python" ]; then
    echo "Creating the virtual environment ($VENV) - first start only ..."
    "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install -q --disable-pip-version-check -r requirements.txt \
    || echo "Warning: could not install/verify the requirements (offline?) - trying to start anyway."
echo "Starting IBKR Portfolio Tracker on http://localhost:${PORT}  (Ctrl+C to stop)"
"$VENV/bin/python" scripts/open_when_ready.py "$PORT" >/dev/null 2>&1 &
exec "$VENV/bin/python" -m streamlit run app.py --server.port "$PORT" "$@"
