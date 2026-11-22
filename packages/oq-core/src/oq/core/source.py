"""Source file model with accurate offset, line, and column mapping.

Every Querly source file is held as immutable text plus a precomputed table of
line start offsets. The mapping is built once from the language's line-break
definition (``\\r\\n``, ``\\r`` or ``\\n``), so CRLF files, LF files, and mixed
files all produce the same logical line numbers.

Binary content is rejected explicitly: a UTF-8 BOM is accepted and removed on
read, but NUL characters indicate a binary or mis-encoded file and raise
:class:`SourceError` instead of being tokenized as garbage.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from oq.core._text import LINE_BREAK_PATTERN
from oq.core.diagnostics import SourceLocation, SourceSpan

__all__ = ["DEFAULT_ENCODING", "SourceError", "SourceFile"]

DEFAULT_ENCODING: Final = "utf-8-sig"
"""Default file encoding: UTF-8 with an optional byte-order mark."""


class SourceError(ValueError):
    """Raised when source text or a source file cannot be read safely."""


@dataclass(frozen=True, slots=True)
class SourceFile:
    """Immutable source text with precomputed line/column lookup."""

    name: str
    text: str
    _line_starts: tuple[int, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("source file name must be a non-empty string")
        if not isinstance(self.text, str):
            msg = f"source text must be str, got {type(self.text).__name__}"
            raise TypeError(msg)
        if "\x00" in self.text:
            msg = (
                f"{self.name}: source contains a NUL character at offset "
                f"{self.text.index(chr(0))}; binary or mis-encoded files are not "
                "valid Querly source"
            )
            raise SourceError(msg)
        starts = (0, *(match.end() for match in LINE_BREAK_PATTERN.finditer(self.text)))
        object.__setattr__(self, "_line_starts", starts)

    @classmethod
    def from_text(cls, text: str, *, name: str = "<memory>") -> SourceFile:
        """Create a source file from in-memory text."""
        return cls(name=name, text=text)

    @classmethod
    def from_path(cls, path: str | Path, *, encoding: str = DEFAULT_ENCODING) -> SourceFile:
        """Read and decode a source file from disk.

        Raises :class:`SourceError` for unreadable files, undecodable bytes, and
        binary content.
        """
        source_path = Path(path)
        try:
            data = source_path.read_bytes()
        except OSError as exc:
            msg = f"cannot read source file {source_path}: {exc}"
            raise SourceError(msg) from exc
        try:
            text = data.decode(encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            msg = f"cannot decode {source_path} as {encoding}: {exc}"
            raise SourceError(msg) from exc
        return cls.from_text(text, name=str(source_path))

    @property
    def length(self) -> int:
        """Length of the source in Unicode code points."""
        return len(self.text)

    @property
    def line_count(self) -> int:
        """Number of logical lines; always at least one."""
        return len(self._line_starts)

    @property
    def line_starts(self) -> tuple[int, ...]:
        """Zero-based offsets where each logical line begins."""
        return self._line_starts

    def location_at(self, offset: int) -> SourceLocation:
        """Map a zero-based code-point offset to a one-based line and column."""
        if not 0 <= offset <= self.length:
            msg = f"{self.name}: offset {offset} is outside 0..{self.length}"
            raise ValueError(msg)
        index = bisect.bisect_right(self._line_starts, offset) - 1
        line_start = self._line_starts[index]
        return SourceLocation(offset=offset, line=index + 1, column=offset - line_start + 1)

    def span(self, start: int, end: int | None = None) -> SourceSpan:
        """Build a half-open span from offsets.

        ``end`` defaults to ``start``, producing an empty insertion span.
        """
        if end is None:
            end = start
        if end < start:
            msg = f"{self.name}: span end {end} precedes start {start}"
            raise ValueError(msg)
        return SourceSpan(
            file=self.name,
            start=self.location_at(start),
            end=self.location_at(end),
        )

    def line_text(self, line: int) -> str:
        """Return the text of a one-based line without its terminator."""
        if not 1 <= line <= self.line_count:
            msg = f"{self.name}: line {line} is outside 1..{self.line_count}"
            raise ValueError(msg)
        start = self._line_starts[line - 1]
        end = self._line_starts[line] if line < self.line_count else self.length
        raw = self.text[start:end]
        if raw.endswith("\r\n"):
            return raw[:-2]
        if raw.endswith(("\r", "\n")):
            return raw[:-1]
        return raw

    def text_of(self, span: SourceSpan) -> str:
        """Return the source text covered by ``span``.

        Raises :class:`SourceError` if the span belongs to another file or
        extends past the end of this text.
        """
        if span.file != self.name:
            msg = f"span belongs to {span.file}, not {self.name}"
            raise SourceError(msg)
        if span.end.offset > self.length:
            msg = (
                f"{self.name}: span end offset {span.end.offset} exceeds text length {self.length}"
            )
            raise SourceError(msg)
        return self.text[span.start.offset : span.end.offset]

    def __str__(self) -> str:
        return self.name
