#!/usr/bin/env bash
# Runs once after the dev container has been created (devcontainer.json -> postCreateCommand).
# It never fails the container creation: problems are reported and the container stays usable.
set -u
cd "$(dirname "$0")/.." || exit 1

echo "==> Python packages"
# The image already contains them. This only installs what changed in requirements*.txt since the image was built
# (and does nothing - without network access - when everything is there).
pip install -q -r requirements-dev.txt \
    || echo "WARNING: could not verify the Python packages (offline?) - the image keeps the versions it was built with."

# A bind-mounted workspace may belong to another user id (Windows / macOS hosts): keep git quiet about it.
git config --global --add safe.directory "$PWD" 2>/dev/null || true

echo
echo "==> Environment"
python - <<'PY'
import importlib.metadata as md
import os
import platform
import sys

print(f"Python        {platform.python_version()}  ({sys.prefix})")
for pkg in ("streamlit", "openpyxl", "pandas", "yfinance", "pytest"):
    try:
        print(f"{pkg:<13} {md.version(pkg)}")
    except md.PackageNotFoundError:
        print(f"{pkg:<13} MISSING")
try:
    import playwright  # noqa: F401
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    found = os.path.isdir(root) and any(n.startswith("chromium") for n in os.listdir(root))
    print("Chromium      " + ("installed (browser smoke test enabled)" if found else
                              "Playwright is installed but Chromium is not: run  playwright install --with-deps chromium"))
except ImportError:
    print("Chromium      not installed (optional: the browser smoke test is skipped; see the README, 'Dev container')")
PY
if command -v soffice >/dev/null 2>&1; then
    echo "LibreOffice   $(soffice --version 2>/dev/null | head -n 1)"
else
    echo "LibreOffice   not installed (optional: the workbook then has no cached values; the tests that need it are skipped)"
fi

cat <<'MSG'

Ready.
  * Start the app   F5 (Run and Debug > "Streamlit: IBKR Portfolio Tracker")  or  ./run_app.sh   ->  http://localhost:8501
  * Run the tests   Terminal > Run Task > "Tests: all"   or   python -m pytest
  * Your statements, database and workbooks stay in ./data of this folder.
MSG
