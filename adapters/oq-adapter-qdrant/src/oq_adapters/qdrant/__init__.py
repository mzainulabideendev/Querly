"""Qdrant adapter for Querly.

Data model: vector. Driver: qdrant-client>=1.9 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.qdrant.adapter import QdrantAdapter

__version__ = "0.1.0.dev0"

__all__ = ["QdrantAdapter", "__version__"]
