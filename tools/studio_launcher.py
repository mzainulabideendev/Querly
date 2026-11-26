"""Standalone launcher for Querly Studio (Backend + Frontend).

This script launches the FastAPI backend and opens the default web browser.
It is intended to be packaged with PyInstaller into a standalone executable.
"""

import sys
import os
import multiprocessing
import uvicorn
import webbrowser
import time
from pathlib import Path

# Add project roots to sys.path to ensure imports work in dev and frozen modes
base_path = Path(__file__).parent.parent
sys.path.append(str(base_path / "packages" / "oq-core" / "src"))
sys.path.append(str(base_path / "packages" / "oq-ir" / "src"))
sys.path.append(str(base_path / "packages" / "oq-types" / "src"))
sys.path.append(str(base_path / "packages" / "oq-compiler" / "src"))
sys.path.append(str(base_path / "packages" / "oq-converter" / "src"))
sys.path.append(str(base_path / "packages" / "oq-runtime" / "src"))
sys.path.append(str(base_path / "studio" / "backend"))

def open_browser(url: str):
    """Wait for the server to start and then open the browser."""
    time.sleep(1.5)  # Give uvicorn some time to start
    print(f"Opening {url} in your default browser...")
    webbrowser.open(url)

def main():
    """Main entry point."""
    host = "127.0.0.1"
    port = 8765
    url = f"http://{host}:{port}"

    # Required for PyInstaller + multiprocessing on Windows
    multiprocessing.freeze_support()

    print("=" * 60)
    print("  Querly Studio - Universal Database IDE")
    print("=" * 60)
    print(f"Starting backend at {url}")

    # Start browser in a separate thread/process
    p = multiprocessing.Process(target=open_browser, args=(url,))
    p.start()

    # Start the FastAPI server
    try:
        uvicorn.run(
            "app:app",
            host=host,
            port=port,
            log_level="info",
            reload=False,
            workers=1
        )
    except KeyboardInterrupt:
        print("\nShutting down Querly Studio...")
    finally:
        p.terminate()

if __name__ == "__main__":
    main()
