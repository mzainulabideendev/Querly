"""Shared lexical text helpers.

The language defines exactly three line terminators: ``\\r\\n``, ``\\r`` and
``\\n``. Every component that maps offsets to lines must agree on those three
sequences, otherwise diagnostics and spans drift apart. This module is the
single definition used by the source model and the diagnostic renderer.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["LINE_BREAK_PATTERN", "split_source_lines"]

LINE_BREAK_PATTERN: Final = re.compile(r"\r\n|\r|\n")


def split_source_lines(text: str) -> list[str]:
    """Split ``text`` into lines using the Querly line-break definition.

    The result always contains at least one element. Text ending in a line
    break yields a final empty line, which keeps line numbers consistent with
    the offset-to-line mapping in :class:`oq.core.source.SourceFile`.
    """
    return LINE_BREAK_PATTERN.split(text)
