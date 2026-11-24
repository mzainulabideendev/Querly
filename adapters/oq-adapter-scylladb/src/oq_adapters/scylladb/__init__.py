"""ScyllaDB adapter for Querly.

Data model: wide-column. Driver: cassandra-driver>=3.29 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.scylladb.adapter import ScyllaDBAdapter

__version__ = "0.1.0.dev0"

__all__ = ["ScyllaDBAdapter", "__version__"]
