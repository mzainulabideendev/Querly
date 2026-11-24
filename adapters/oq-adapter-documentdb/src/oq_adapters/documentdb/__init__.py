"""Amazon DocumentDB adapter for Querly.

Data model: document. Driver: pymongo>=4.6 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.documentdb.adapter import DocumentDBAdapter

__version__ = "0.1.0.dev0"

__all__ = ["DocumentDBAdapter", "__version__"]
