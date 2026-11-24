"""Amazon Neptune adapter for Querly.

Data model: graph. Driver: gremlinpython>=3.7 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.neptune.adapter import NeptuneAdapter

__version__ = "0.1.0.dev0"

__all__ = ["NeptuneAdapter", "__version__"]
