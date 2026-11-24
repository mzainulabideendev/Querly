"""Microsoft SQL Server adapter for Querly.

Data model: relational. Driver: pyodbc>=5.1 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.sqlserver.adapter import SQLServerAdapter

__version__ = "0.1.0.dev0"

__all__ = ["SQLServerAdapter", "__version__"]
