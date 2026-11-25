"""SQLite adapter for Querly.

This adapter provides full Querly adapter contract compliance for SQLite,
which is the local baseline engine with no external infrastructure requirements.

The adapter uses Python's built-in sqlite3 module (no extra install needed).
"""

from __future__ import annotations

__version__ = "0.1.0.dev0"

from oq_adapters.sqlite.adapter import SQLiteAdapter

__all__ = ["SQLiteAdapter", "__version__"]
