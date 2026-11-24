"""ClickHouse adapter for Querly.

Data model: analytical. Driver: clickhouse-connect>=0.7 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.clickhouse.adapter import ClickHouseAdapter

__version__ = "0.1.0.dev0"

__all__ = ["ClickHouseAdapter", "__version__"]
