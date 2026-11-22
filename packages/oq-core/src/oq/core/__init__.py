"""Querly language core.

This package owns the parts of the pipeline that must never depend on a
database driver: source text handling, diagnostics, lexical analysis, parsing,
the abstract syntax tree, and semantic analysis.

Phase 0 provides the source model (:mod:`oq.core.source`) and the diagnostic
model (:mod:`oq.core.diagnostics`). The lexer, parser, AST, and semantic
analyzer are implemented in Phases 1-3; they are not present yet and this
package does not claim to provide them.
"""

from __future__ import annotations

from oq.core.diagnostics import (
    Diagnostic,
    DiagnosticBag,
    DiagnosticError,
    Severity,
    SourceLocation,
    SourceSpan,
    render_diagnostic,
)
from oq.core.source import DEFAULT_ENCODING, SourceError, SourceFile
from oq.core.version import VERSION, VERSION_INFO, version_string

__all__ = [
    "DEFAULT_ENCODING",
    "VERSION",
    "VERSION_INFO",
    "Diagnostic",
    "DiagnosticBag",
    "DiagnosticError",
    "Severity",
    "SourceError",
    "SourceFile",
    "SourceLocation",
    "SourceSpan",
    "render_diagnostic",
    "version_string",
]
