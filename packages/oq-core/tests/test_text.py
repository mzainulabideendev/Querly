"""Tests for the shared line-break definition."""

from __future__ import annotations

import pytest
from oq.core._text import split_source_lines


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", [""]),
        ("a", ["a"]),
        ("a\n", ["a", ""]),
        ("a\nb", ["a", "b"]),
        ("a\r\nb", ["a", "b"]),
        ("a\rb", ["a", "b"]),
        ("a\r\n\r\nb", ["a", "", "b"]),
        ("\n", ["", ""]),
        ("a\n\rb", ["a", "", "b"]),
    ],
)
def test_split_source_lines(text: str, expected: list[str]) -> None:
    assert split_source_lines(text) == expected
