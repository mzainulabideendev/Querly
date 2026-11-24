"""Neo4j adapter for Querly.

Data model: graph. Driver: neo4j>=5.20 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.neo4j.adapter import Neo4jAdapter

__version__ = "0.1.0.dev0"

__all__ = ["Neo4jAdapter", "__version__"]
