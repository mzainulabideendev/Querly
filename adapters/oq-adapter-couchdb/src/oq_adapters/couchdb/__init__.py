"""Apache CouchDB adapter for Querly.

Data model: document. Driver: pycouchdb>=1.14 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.couchdb.adapter import CouchDBAdapter

__version__ = "0.1.0.dev0"

__all__ = ["CouchDBAdapter", "__version__"]
