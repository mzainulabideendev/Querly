"""CockroachDB adapter for Querly.

Data model: relational. Driver: psycopg[binary]>=3.1 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.cockroachdb.adapter import CockroachDBAdapter

__version__ = "0.1.0.dev0"

__all__ = ["CockroachDBAdapter", "__version__"]
