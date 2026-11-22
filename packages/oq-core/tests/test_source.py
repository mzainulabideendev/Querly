"""Tests for the source model and offset/line/column mapping."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from oq.core._text import split_source_lines
from oq.core.diagnostics import SourceLocation, SourceSpan
from oq.core.source import SourceError, SourceFile


def test_empty_source_has_one_empty_line() -> None:
    src = SourceFile.from_text("")
    assert src.length == 0
    assert src.line_count == 1
    assert src.line_starts == (0,)
    assert src.line_text(1) == ""
    assert src.location_at(0) == SourceLocation(offset=0, line=1, column=1)


def test_lf_mapping() -> None:
    src = SourceFile.from_text("one\ntwo\nthree", name="a.oq")
    assert src.line_count == 3
    assert src.location_at(0) == SourceLocation(0, 1, 1)
    assert src.location_at(3) == SourceLocation(3, 1, 4)
    assert src.location_at(4) == SourceLocation(4, 2, 1)
    assert src.location_at(8) == SourceLocation(8, 3, 1)
    assert src.line_text(2) == "two"


def test_crlf_mapping_treats_break_as_one_terminator() -> None:
    src = SourceFile.from_text("a\r\nb\r\n", name="crlf.oq")
    assert src.line_count == 3
    assert src.line_starts == (0, 3, 6)
    assert src.location_at(1) == SourceLocation(1, 1, 2)
    assert src.location_at(2) == SourceLocation(2, 1, 3)
    assert src.location_at(3) == SourceLocation(3, 2, 1)
    assert src.line_text(1) == "a"
    assert src.line_text(2) == "b"
    assert src.line_text(3) == ""


def test_cr_only_mapping() -> None:
    src = SourceFile.from_text("a\rb\rc", name="old-mac.oq")
    assert src.line_count == 3
    assert src.line_starts == (0, 2, 4)
    assert src.line_text(2) == "b"


def test_mixed_line_breaks() -> None:
    src = SourceFile.from_text("a\r\nb\nc\rd", name="mixed.oq")
    assert src.line_starts == (0, 3, 5, 7)
    assert [src.line_text(n) for n in range(1, 5)] == ["a", "b", "c", "d"]


def test_unicode_columns_count_code_points() -> None:
    src = SourceFile.from_text("héllo\n🙂x", name="unicode.oq")
    assert src.length == 8
    assert src.location_at(1) == SourceLocation(1, 1, 2)
    assert src.location_at(6) == SourceLocation(6, 2, 1)
    assert src.location_at(7) == SourceLocation(7, 2, 2)
    assert src.location_at(8) == SourceLocation(8, 2, 3)
    assert src.line_text(2) == "🙂x"


def test_span_and_text_of() -> None:
    src = SourceFile.from_text("create table users", name="schema.oq")
    span = src.span(13, 18)
    assert span == SourceSpan(
        file="schema.oq",
        start=SourceLocation(13, 1, 14),
        end=SourceLocation(18, 1, 19),
    )
    assert span.length == 5
    assert src.text_of(span) == "users"
    assert span.slice_of(src.text) == "users"
    assert str(span) == "schema.oq:1:14"


def test_zero_length_span_is_allowed() -> None:
    src = SourceFile.from_text("abc")
    span = src.span(1)
    assert span.length == 0
    assert src.text_of(span) == ""


def test_offset_and_span_bounds_are_enforced() -> None:
    src = SourceFile.from_text("abc", name="bounds.oq")
    with pytest.raises(ValueError, match=r"outside 0\.\.3"):
        src.location_at(4)
    with pytest.raises(ValueError, match=r"outside 0\.\.3"):
        src.location_at(-1)
    with pytest.raises(ValueError, match="precedes start"):
        src.span(2, 1)
    with pytest.raises(ValueError, match=r"outside 1\.\.1"):
        src.line_text(2)


def test_text_of_rejects_foreign_span() -> None:
    src = SourceFile.from_text("abc", name="a.oq")
    other = SourceFile.from_text("abc", name="b.oq")
    with pytest.raises(SourceError, match=r"belongs to a\.oq"):
        other.text_of(src.span(0, 1))


def test_nul_character_is_rejected() -> None:
    with pytest.raises(SourceError, match="NUL character"):
        SourceFile.from_text("abc\x00def", name="binary.oq")


def test_non_string_text_is_rejected() -> None:
    with pytest.raises(TypeError, match="must be str"):
        SourceFile(name="x.oq", text=b"bytes")  # type: ignore[arg-type]


def test_empty_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="name"):
        SourceFile.from_text("x", name="  ")


def test_from_path_reads_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "bom.oq"
    path.write_bytes(b"\xef\xbb\xbfcreate table t")
    src = SourceFile.from_path(path)
    assert src.text == "create table t"
    assert src.name == str(path)


def test_from_path_reports_invalid_utf8(tmp_path: Path) -> None:
    path = tmp_path / "invalid.oq"
    path.write_bytes(b"abc\xff\xfe")
    with pytest.raises(SourceError, match="cannot decode"):
        SourceFile.from_path(path)


def test_from_path_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="cannot read source file"):
        SourceFile.from_path(tmp_path / "missing.oq")


def test_source_file_is_hashable_and_frozen() -> None:
    src = SourceFile.from_text("abc")
    assert src == SourceFile.from_text("abc")
    assert hash(src) == hash(SourceFile.from_text("abc"))
    with pytest.raises(AttributeError):
        src.text = "other"  # type: ignore[misc]


_ALPHABET = st.characters(blacklist_characters="\x00")


@st.composite
def _text_and_offset(draw: st.DrawFn) -> tuple[str, int]:
    text = draw(st.text(alphabet=_ALPHABET, max_size=40))
    offset = draw(st.integers(min_value=0, max_value=len(text)))
    return text, offset


@settings(max_examples=200, deadline=None)
@given(case=_text_and_offset())
def test_location_mapping_is_consistent(case: tuple[str, int]) -> None:
    text, offset = case
    src = SourceFile.from_text(text)
    location = src.location_at(offset)
    assert 1 <= location.line <= src.line_count
    assert location.offset == offset
    assert src.line_starts[location.line - 1] + location.column - 1 == offset
    assert src.line_text(location.line) == split_source_lines(text)[location.line - 1]


@st.composite
def _text_and_span(draw: st.DrawFn) -> tuple[str, int, int]:
    text = draw(st.text(alphabet=_ALPHABET, max_size=40))
    start = draw(st.integers(min_value=0, max_value=len(text)))
    end = draw(st.integers(min_value=start, max_value=len(text)))
    return text, start, end


@settings(max_examples=200, deadline=None)
@given(case=_text_and_span())
def test_span_roundtrip(case: tuple[str, int, int]) -> None:
    text, start, end = case
    src = SourceFile.from_text(text)
    span = src.span(start, end)
    assert span.length == end - start
    assert src.text_of(span) == text[start:end]
    assert span.slice_of(text) == text[start:end]
