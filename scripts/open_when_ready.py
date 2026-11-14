"""Open the default browser as soon as the Streamlit server answers (used by run_app.bat / run_app.sh).

    python scripts/open_when_ready.py [port]
"""
from __future__ import annotations

import sys
import time
import urllib.request
import webbrowser


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8501
    url = f"http://localhost:{port}"
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            if urllib.request.urlopen(f"{url}/_stcore/health", timeout=1).read() == b"ok":
                webbrowser.open(url)
                return 0
        except Exception:                                   # noqa: BLE001 - server not up yet
            time.sleep(0.5)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
