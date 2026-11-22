"""Querly universal intermediate representation (IR).

Phase 0 scaffold. This distribution exists so the workspace layout and
installation are verifiable. The versioned IR models (schema, entities,
attributes, queries, expressions, plans, migrations), Pydantic validation, and
versioned JSON serialization arrive in Phase 2 and are specified in
``docs/ir/IR_SPECIFICATION.md``.

No IR is produced or consumed yet. Only the format version constant below is
public, and it identifies the document format this package will implement.
"""

from __future__ import annotations

from typing import Final

__all__ = ["IR_FORMAT_VERSION"]

IR_FORMAT_VERSION: Final = "0.1"
"""Document format version written into serialized Querly IR documents."""
