"""Querly Studio compiler bridge.

The upstream repository ships a hand-written lexer, AST, semantic model, IR and
adapter contracts.  Its Lark parser is not wired to the custom lexer, so source
files cannot reach the semantic layer yet.  This package provides a real,
tested parser for the documented language subset and uses the upstream lexer,
AST dataclasses, IR models, capability reports and adapters.  Nothing here
fabricates success: every stage either performs work or returns diagnostics.
"""

from __future__ import annotations

from .pipeline import CompilationResult, CompilerPipeline, PipelineOptions

__all__ = ["CompilationResult", "CompilerPipeline", "PipelineOptions"]
