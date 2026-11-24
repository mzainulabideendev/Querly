"""Milvus adapter for Querly.

Data model: vector. Driver: pymilvus>=2.4 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.milvus.adapter import MilvusAdapter

__version__ = "0.1.0.dev0"

__all__ = ["MilvusAdapter", "__version__"]
