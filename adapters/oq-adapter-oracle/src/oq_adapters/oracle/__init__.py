"""Oracle Database adapter for Querly.

Data model: relational. Driver: oracledb>=2.0 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.oracle.adapter import OracleAdapter

__version__ = "0.1.0.dev0"

__all__ = ["OracleAdapter", "__version__"]
