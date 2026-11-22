"""Source locations, diagnostics, and human-readable diagnostic rendering.

This module is part of the Querly language core. It has no database
dependencies and no third-party dependencies, so every package in the
workspace can report problems in one uniform format.

Design rules encoded here:

* ``offset`` is a zero-based index into the source text, counted in Unicode
  code points (Python string indices). ``line`` and ``column`` are one-based.
* A span is half-open: ``start`` is included and ``end`` is excluded.
* Diagnostics are immutable values; collections of them are managed by
  :class:`DiagnosticBag`.
* Diagnostic codes are stable identifiers of the form ``OQ-AREA-NNN``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from oq.core._text import split_source_lines

__all__ = [
    "Diagnostic",
    "DiagnosticBag",
    "DiagnosticError",
    "Severity",
    "SourceLocation",
    "SourceSpan",
    "render_diagnostic",
]

CODE_PATTERN: Final = re.compile(r"^OQ-[A-Z]{2,6}-\d{3}$")
"""Valid diagnostic codes look like ``OQ-SRC-001`` or ``OQ-PARSE-014``."""


class Severity(StrEnum):
    """Severity of a diagnostic, ordered from most to least urgent."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    HINT = "hint"


SEVERITY_RANK: Final[dict[Severity, int]] = {
    Severity.ERROR: 0,
    Severity.WARNING: 1,
    Severity.INFO: 2,
    Severity.HINT: 3,
}


@dataclass(frozen=True, slots=True, order=True)
class SourceLocation:
    """A single position in a source file.

    ``offset`` is zero-based; ``line`` and ``column`` are one-based. Columns
    count Unicode code points, not bytes and not display cells.
    """

    offset: int
    line: int
    column: int

    def __post_init__(self) -> None:
        if self.offset < 0:
            msg = f"offset must be >= 0, got {self.offset}"
            raise ValueError(msg)
        if self.line < 1:
            msg = f"line must be >= 1, got {self.line}"
            raise ValueError(msg)
        if self.column < 1:
            msg = f"column must be >= 1, got {self.column}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A half-open range of source text within a named file."""

    file: str
    start: SourceLocation
    end: SourceLocation

    def __post_init__(self) -> None:
        if not self.file:
            msg = "span file name must be non-empty"
            raise ValueError(msg)
        if self.start.offset > self.end.offset:
            msg = f"span start offset {self.start.offset} is after end offset {self.end.offset}"
            raise ValueError(msg)
        if self.start.line > self.end.line:
            msg = f"span start line {self.start.line} is after end line {self.end.line}"
            raise ValueError(msg)
        if self.start.line == self.end.line and self.start.column > self.end.column:
            msg = (
                f"span start column {self.start.column} is after end column "
                f"{self.end.column} on line {self.start.line}"
            )
            raise ValueError(msg)

    @property
    def length(self) -> int:
        """Number of code points covered by the span."""
        return self.end.offset - self.start.offset

    def slice_of(self, text: str) -> str:
        """Return the covered portion of ``text``.

        The caller is responsible for passing the text the span was created
        from. Offsets outside ``text`` raise :class:`ValueError` rather than
        silently returning a shorter string.
        """
        if self.end.offset > len(text):
            msg = f"span end offset {self.end.offset} exceeds text length {len(text)} for {self}"
            raise ValueError(msg)
        return text[self.start.offset : self.end.offset]

    def __str__(self) -> str:
        return f"{self.file}:{self.start.line}:{self.start.column}"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """A single problem report with a stable code and optional location."""

    code: str
    severity: Severity
    message: str
    span: SourceSpan | None = None
    notes: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if not CODE_PATTERN.match(self.code):
            msg = (
                f"invalid diagnostic code {self.code!r}; expected the form "
                "'OQ-AREA-NNN' with an uppercase area of 2-6 letters"
            )
            raise ValueError(msg)
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("diagnostic message must be a non-empty string")
        notes = tuple(self.notes)
        for note in notes:
            if not isinstance(note, str) or not note.strip():
                raise ValueError("diagnostic notes must be non-empty strings")
        if notes != self.notes:
            object.__setattr__(self, "notes", notes)

    @property
    def is_error(self) -> bool:
        """Whether this diagnostic prevents successful compilation."""
        return self.severity is Severity.ERROR

    def with_note(self, note: str) -> Diagnostic:
        """Return a copy of this diagnostic with an additional note."""
        return Diagnostic(
            code=self.code,
            severity=self.severity,
            message=self.message,
            span=self.span,
            notes=(*self.notes, note),
        )

    def render(self, source_text: str | None = None) -> str:
        """Render this diagnostic, optionally with the offending source line."""
        return render_diagnostic(self, source_text)


def _line_at(source_text: str, line: int) -> str | None:
    lines = split_source_lines(source_text)
    if 1 <= line <= len(lines):
        return lines[line - 1]
    return None


def _underline(span: SourceSpan, line_text: str) -> str:
    start = span.start.column - 1
    if span.end.line == span.start.line:
        length = max(1, span.end.column - span.start.column)
    else:
        length = max(1, len(line_text) - start)
    return " " * start + "^" + "~" * (length - 1)


def render_diagnostic(diagnostic: Diagnostic, source_text: str | None = None) -> str:
    """Render ``diagnostic`` in a compiler-style, terminal-friendly format.

    Example::

        error[OQ-PARSE-004]: expected ')' after parameter list
          --> app.oq:12:18
           |
        12 | query find(id: integer {
           |                      ^
           = note: function parameters are parenthesized
    """
    header = f"{diagnostic.severity.value}[{diagnostic.code}]: {diagnostic.message}"
    lines: list[str] = [header]
    span = diagnostic.span
    if span is not None:
        lines.append(f"  --> {span}")
        if source_text is not None:
            line_text = _line_at(source_text, span.start.line)
            if line_text is not None:
                width = len(str(span.start.line))
                bar = " " * (width + 2) + "|"
                lines.append(bar)
                lines.append(f"{span.start.line:>{width + 1}} | {line_text}")
                lines.append(f"{bar} {_underline(span, line_text)}")
    for note in diagnostic.notes:
        lines.append(f"  = note: {note}")
    return "\n".join(lines)


class DiagnosticBag:
    """An ordered, mutable collection of diagnostics.

    The bag accepts every severity. Compilation fails only when the bag
    contains at least one error.
    """

    __slots__ = ("_items",)

    def __init__(self, diagnostics: Iterable[Diagnostic] = ()) -> None:
        self._items: list[Diagnostic] = list(diagnostics)

    def add(self, diagnostic: Diagnostic) -> None:
        """Append a diagnostic."""
        if not isinstance(diagnostic, Diagnostic):
            msg = f"expected Diagnostic, got {type(diagnostic).__name__}"
            raise TypeError(msg)
        self._items.append(diagnostic)

    def extend(self, diagnostics: Iterable[Diagnostic]) -> None:
        """Append every diagnostic from ``diagnostics``."""
        for diagnostic in diagnostics:
            self.add(diagnostic)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Diagnostic]:
        return iter(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        """A snapshot of the current diagnostics, in insertion order."""
        return tuple(self._items)

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        """Only the error-severity diagnostics."""
        return tuple(item for item in self._items if item.is_error)

    @property
    def warnings(self) -> tuple[Diagnostic, ...]:
        """Only the warning-severity diagnostics."""
        return tuple(item for item in self._items if item.severity is Severity.WARNING)

    @property
    def has_errors(self) -> bool:
        """Whether at least one error has been recorded."""
        return any(item.is_error for item in self._items)

    def sorted_diagnostics(self) -> tuple[Diagnostic, ...]:
        """Return diagnostics ordered by file, offset, severity, then code.

        Diagnostics without a span are ordered last. The sort is stable, so
        insertion order breaks any remaining ties.
        """

        def key(item: Diagnostic) -> tuple[str, int, int, str]:
            if item.span is None:
                return ("\U0010ffff", 1 << 62, SEVERITY_RANK[item.severity], item.code)
            span = item.span
            return (span.file, span.start.offset, SEVERITY_RANK[item.severity], item.code)

        return tuple(sorted(self._items, key=key))

    def render(self, source_text: str | None = None, *, sort: bool = True) -> str:
        """Render all diagnostics, separated by newlines."""
        selected = self.sorted_diagnostics() if sort else self.diagnostics
        return "\n".join(item.render(source_text) for item in selected)

    def raise_if_errors(self) -> None:
        """Raise :class:`DiagnosticError` when any error was recorded."""
        if self.has_errors:
            raise DiagnosticError(self._items)


class DiagnosticError(Exception):
    """Raised when compilation stops because errors were recorded."""

    def __init__(self, diagnostics: Iterable[Diagnostic]) -> None:
        items = tuple(diagnostics)
        if not items:
            msg = "DiagnosticError requires at least one diagnostic"
            raise ValueError(msg)
        self.diagnostics: tuple[Diagnostic, ...] = items
        errors = tuple(item for item in items if item.is_error)
        count = len(errors)
        noun = "error" if count == 1 else "errors"
        primary = errors[0] if errors else items[0]
        super().__init__(f"{count} {noun}; first: {primary.code}: {primary.message}")
