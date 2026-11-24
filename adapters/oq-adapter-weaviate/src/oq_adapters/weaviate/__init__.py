"""Weaviate adapter for Querly.

Data model: vector. Driver: weaviate-client>=4.5 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.weaviate.adapter import WeaviateAdapter

__version__ = "0.1.0.dev0"

__all__ = ["WeaviateAdapter", "__version__"]
