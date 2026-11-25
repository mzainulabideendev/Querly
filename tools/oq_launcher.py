"""Entry point used by PyInstaller when building the standalone ``oq`` binary."""

from __future__ import annotations

from oq.cli import main

raise SystemExit(main())
