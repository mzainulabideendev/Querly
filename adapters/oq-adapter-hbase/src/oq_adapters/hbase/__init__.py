"""Apache HBase adapter for Querly.

Data model: wide-column. Driver: happybase>=1.2 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.hbase.adapter import HBaseAdapter

__version__ = "0.1.0.dev0"

__all__ = ["HBaseAdapter", "__version__"]
