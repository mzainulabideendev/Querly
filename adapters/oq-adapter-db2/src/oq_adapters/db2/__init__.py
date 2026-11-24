"""IBM Db2 adapter for Querly.

Data model: relational. Driver: ibm-db>=3.2 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.db2.adapter import Db2Adapter

__version__ = "0.1.0.dev0"

__all__ = ["Db2Adapter", "__version__"]
