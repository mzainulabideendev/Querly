"""Querly compiler: normalization, capability analysis, planning, code generation.

Phase 0 scaffold. This distribution exists so the workspace layout and
installation are verifiable. The compiler pipeline (AST to semantic model to
universal IR to capability-checked physical plan) is implemented in Phase 3 and
later phases, following ``ARCHITECTURE.md``.

No compilation is implemented yet and no target SQL is generated.
"""

from __future__ import annotations

__all__: list[str] = []
