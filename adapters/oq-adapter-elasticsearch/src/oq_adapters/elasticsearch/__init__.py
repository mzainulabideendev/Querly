"""Elasticsearch adapter for Querly.

Data model: search. Driver: elasticsearch>=8.13,<9 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.elasticsearch.adapter import ElasticsearchAdapter

__version__ = "0.1.0.dev0"

__all__ = ["ElasticsearchAdapter", "__version__"]
