"""Standalone launcher for Querly Studio (backend + bundled frontend).

This module is the PyInstaller entry point. It starts the FastAPI backend and
opens the IDE in the default browser. The same file also works when run
directly from a source checkout.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _bundle_root() -> Path:
    """Return the directory that holds the bundled application files."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return Path(__file__).resolve().parent.parent


def _bootstrap_paths() -> None:
    """Make ``app``/``querly_studio`` importable in source checkouts."""
    root = _bundle_root()
    candidates = [root / "studio" / "backend", root]
    packages = root / "packages"
    if packages.is_dir():
        candidates.extend(sorted(packages.glob("*/src")))

    for candidate in reversed(candidates):
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))


_bootstrap_paths()

import uvicorn  # noqa: E402
from app import app  # noqa: E402  (path bootstrap must run first)
from querly_studio import APP_NAME, __version__  # noqa: E402


def _open_browser(url: str, delay: float = 1.5) -> None:
    """Wait briefly for the server to bind, then open the browser."""
    time.sleep(delay)
    print(f"Opening {url} in your default browser...")
    webbrowser.open(url)


def _port() -> int:
    raw = os.environ.get("QUERLY_STUDIO_PORT")
    if raw and raw.isdigit():
        return int(raw)
    return DEFAULT_PORT


def main() -> None:
    multiprocessing.freeze_support()

    if any(arg in {"--version", "-V", "version"} for arg in sys.argv[1:]):
        print(f"{APP_NAME} {__version__}")
        return

    port = _port()
    url = f"http://{HOST}:{port}"

    print("=" * 60)
    print(f"  {APP_NAME} - Universal Database IDE")
    print("=" * 60)
    print(f"Starting backend at {url}")

    if os.environ.get("QUERLY_STUDIO_NO_BROWSER") != "1":
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()

    try:
        uvicorn.run(app, host=HOST, port=port, log_level="info", workers=1)
    except KeyboardInterrupt:
        print(f"\nShutting down {APP_NAME}...")


if __name__ == "__main__":
    main()
