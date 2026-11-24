"""OpenSearch adapter for Querly.

Data model: search. Driver: opensearch-py>=2.6 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.opensearch.adapter import OpenSearchAdapter

__version__ = "0.1.0.dev0"

__all__ = ["OpenSearchAdapter", "__version__"]
