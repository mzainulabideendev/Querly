"""Aerospike adapter for Querly.

Data model: key-value. Driver: aerospike>=7.0 (imported lazily).
"""

from __future__ import annotations

from oq_adapters.aerospike.adapter import AerospikeAdapter

__version__ = "0.1.0.dev0"

__all__ = ["AerospikeAdapter", "__version__"]
