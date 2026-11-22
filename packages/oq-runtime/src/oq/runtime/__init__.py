"""Querly runtime: connections, execution, transactions, security policy.

Phase 0 scaffold. This distribution exists so the workspace layout and
installation are verifiable. Connection management, parameterized execution,
transaction control, timeouts, read-only enforcement, audit logging, and
result normalization are implemented in Phase 7, against the adapter contract
in ``docs/adapters/ADAPTER_GUIDE.md``.

No database connection can be opened through this package yet.
"""

from __future__ import annotations

__all__: list[str] = []
