"""Tests for diagnostics, diagnostic ordering, and rendering."""

from __future__ import annotations

import pytest
from oq.core.diagnostics import (
    Diagnostic,
    DiagnosticBag,
    DiagnosticError,
    Severity,
    SourceLocation,
    SourceSpan,
    render_diagnostic,
)
from oq.core.source import SourceFile


def _span(src: SourceFile, text: str, *, occurrence: int = 0) -> SourceSpan:
    start = -1
    for _ in range(occurrence + 1):
        start = src.text.index(text, start + 1)
    return src.span(start, start + len(text))


def test_source_location_validation() -> None:
    with pytest.raises(ValueError, match="offset"):
        SourceLocation(-1, 1, 1)
    with pytest.raises(ValueError, match="line"):
        SourceLocation(0, 0, 1)
    with pytest.raises(ValueError, match="column"):
        SourceLocation(0, 1, 0)


def test_source_location_is_ordered() -> None:
    assert SourceLocation(0, 1, 1) < SourceLocation(1, 1, 2)
    assert SourceLocation(2, 1, 3) < SourceLocation(3, 2, 1)


def test_span_validation() -> None:
    src = SourceFile.from_text("abcdef")
    with pytest.raises(ValueError, match="after end offset"):
        SourceSpan("f.oq", src.location_at(2), src.location_at(1))
    with pytest.raises(ValueError, match="after end line"):
        SourceSpan(
            "f.oq",
            SourceLocation(3, 2, 1),
            SourceLocation(4, 1, 2),
        )
    with pytest.raises(ValueError, match="after end column"):
        SourceSpan(
            "f.oq",
            SourceLocation(1, 1, 3),
            SourceLocation(2, 1, 2),
        )
    with pytest.raises(ValueError, match="file name"):
        SourceSpan("", src.location_at(0), src.location_at(0))


def test_span_slice_beyond_text_is_rejected() -> None:
    span = SourceSpan("f.oq", SourceLocation(0, 1, 1), SourceLocation(10, 1, 11))
    with pytest.raises(ValueError, match="exceeds text length"):
        span.slice_of("short")


def test_diagnostic_code_validation() -> None:
    for bad in ["", "PARSE-001", "OQ-parse-001", "OQ-PARSE-1", "OQ_P-001"]:
        with pytest.raises(ValueError, match="invalid diagnostic code"):
            Diagnostic(code=bad, severity=Severity.ERROR, message="boom")
    Diagnostic(code="OQ-SRC-001", severity=Severity.ERROR, message="ok")
    Diagnostic(code="OQ-PARSE-014", severity=Severity.ERROR, message="ok")


def test_diagnostic_message_and_notes_validation() -> None:
    with pytest.raises(ValueError, match="message"):
        Diagnostic(code="OQ-SRC-001", severity=Severity.ERROR, message="   ")
    with pytest.raises(ValueError, match="notes"):
        Diagnostic(code="OQ-SRC-001", severity=Severity.ERROR, message="x", notes=("",))


def test_diagnostic_with_note_returns_new_value() -> None:
    first = Diagnostic(code="OQ-SRC-001", severity=Severity.WARNING, message="x")
    second = first.with_note("see docs")
    assert first.notes == ()
    assert second.notes == ("see docs",)
    assert second.is_error is False


def test_is_error_tracks_severity() -> None:
    assert Diagnostic("OQ-SRC-001", Severity.ERROR, "x").is_error
    assert not Diagnostic("OQ-SRC-001", Severity.WARNING, "x").is_error
    assert not Diagnostic("OQ-SRC-001", Severity.INFO, "x").is_error
    assert not Diagnostic("OQ-SRC-001", Severity.HINT, "x").is_error


def test_render_with_source_line_and_caret() -> None:
    text = "create table users"
    src = SourceFile.from_text(text, name="users.oq")
    diag = Diagnostic(
        code="OQ-PARSE-001",
        severity=Severity.ERROR,
        message="unexpected token",
        span=_span(src, "users"),
        notes=("entity names must be declared before use",),
    )
    rendered = diag.render(text)
    assert rendered == (
        "error[OQ-PARSE-001]: unexpected token\n"
        "  --> users.oq:1:14\n"
        "   |\n"
        " 1 | create table users\n"
        "   |              ^~~~~\n"
        "  = note: entity names must be declared before use"
    )


def test_render_multiline_span_underlines_to_end_of_first_line() -> None:
    text = "abc\ndef"
    src = SourceFile.from_text(text, name="m.oq")
    diag = Diagnostic(
        code="OQ-PARSE-002",
        severity=Severity.ERROR,
        message="multi",
        span=src.span(1, 5),
    )
    rendered = diag.render(text)
    assert "1 | abc" in rendered
    assert "   |  ^~" in rendered


def test_render_without_source_omits_snippet() -> None:
    diag = Diagnostic(
        code="OQ-CORE-001",
        severity=Severity.INFO,
        message="note only",
        span=SourceSpan("x.oq", SourceLocation(0, 1, 1), SourceLocation(0, 1, 1)),
    )
    rendered = diag.render()
    assert rendered == "info[OQ-CORE-001]: note only\n  --> x.oq:1:1"


def test_render_diagnostic_without_span() -> None:
    diag = Diagnostic("OQ-CORE-002", Severity.HINT, "try `oq check`")
    assert render_diagnostic(diag) == "hint[OQ-CORE-002]: try `oq check`"


def test_render_clips_underline_when_source_line_is_missing() -> None:
    diag = Diagnostic(
        code="OQ-CORE-003",
        severity=Severity.ERROR,
        message="stale span",
        span=SourceSpan("x.oq", SourceLocation(50, 9, 1), SourceLocation(51, 9, 2)),
    )
    rendered = diag.render("one line only")
    assert "one line only" not in rendered
    assert "  --> x.oq:9:1" in rendered


def test_bag_counts_and_filters() -> None:
    bag = DiagnosticBag(
        [
            Diagnostic("OQ-SRC-001", Severity.ERROR, "e1"),
            Diagnostic("OQ-SRC-002", Severity.WARNING, "w1"),
            Diagnostic("OQ-SRC-003", Severity.INFO, "i1"),
        ]
    )
    assert len(bag) == 3
    assert bool(bag) is True
    assert [d.code for d in bag.errors] == ["OQ-SRC-001"]
    assert [d.code for d in bag.warnings] == ["OQ-SRC-002"]
    assert bag.has_errors is True
    assert list(bag) == list(bag.diagnostics)


def test_empty_bag() -> None:
    bag = DiagnosticBag()
    assert len(bag) == 0
    assert bool(bag) is False
    assert bag.has_errors is False
    assert bag.render() == ""
    bag.raise_if_errors()


def test_bag_rejects_non_diagnostics() -> None:
    bag = DiagnosticBag()
    with pytest.raises(TypeError, match="expected Diagnostic"):
        bag.add("not a diagnostic")  # type: ignore[arg-type]


def test_bag_ordering_places_earlier_offsets_first_then_errors() -> None:
    src = SourceFile.from_text("aaa bbb ccc", name="o.oq")
    later_warning = Diagnostic("OQ-SRC-010", Severity.WARNING, "later", span=src.span(8, 11))
    earlier_error = Diagnostic("OQ-SRC-020", Severity.ERROR, "earlier", span=src.span(0, 3))
    same_offset_hint = Diagnostic("OQ-SRC-030", Severity.HINT, "same offset", span=src.span(0, 3))
    bag = DiagnosticBag([later_warning, earlier_error, same_offset_hint])

    ordered = bag.sorted_diagnostics()
    assert [d.code for d in ordered] == ["OQ-SRC-020", "OQ-SRC-030", "OQ-SRC-010"]
    assert [d.code for d in bag.diagnostics] == ["OQ-SRC-010", "OQ-SRC-020", "OQ-SRC-030"]


def test_bag_orders_spanless_diagnostics_last() -> None:
    with_span = Diagnostic(
        "OQ-SRC-001",
        Severity.WARNING,
        "span",
        span=SourceSpan("z.oq", SourceLocation(0, 1, 1), SourceLocation(0, 1, 1)),
    )
    without_span = Diagnostic("OQ-SRC-002", Severity.ERROR, "no span")
    bag = DiagnosticBag([without_span, with_span])
    assert [d.code for d in bag.sorted_diagnostics()] == ["OQ-SRC-001", "OQ-SRC-002"]


def test_bag_render_respects_sort_flag() -> None:
    src = SourceFile.from_text("aa bb", name="r.oq")
    bag = DiagnosticBag(
        [
            Diagnostic("OQ-SRC-010", Severity.WARNING, "second", span=src.span(3, 5)),
            Diagnostic("OQ-SRC-020", Severity.ERROR, "first", span=src.span(0, 2)),
        ]
    )
    sorted_text = bag.render(src.text)
    insertion_text = bag.render(src.text, sort=False)
    assert sorted_text.index("first") < sorted_text.index("second")
    assert insertion_text.index("second") < insertion_text.index("first")


def test_raise_if_errors_raises_with_summary() -> None:
    bag = DiagnosticBag(
        [
            Diagnostic("OQ-SRC-001", Severity.ERROR, "bad thing"),
            Diagnostic("OQ-SRC-002", Severity.ERROR, "worse thing"),
            Diagnostic("OQ-SRC-003", Severity.WARNING, "warning only"),
        ]
    )
    with pytest.raises(DiagnosticError) as excinfo:
        bag.raise_if_errors()
    assert excinfo.value.diagnostics == bag.diagnostics
    assert str(excinfo.value) == "2 errors; first: OQ-SRC-001: bad thing"


def test_diagnostic_error_requires_diagnostics() -> None:
    with pytest.raises(ValueError, match="at least one diagnostic"):
        DiagnosticError([])


def test_diagnostic_error_prefers_errors_for_summary() -> None:
    error = DiagnosticError(
        [
            Diagnostic("OQ-SRC-001", Severity.WARNING, "warn"),
            Diagnostic("OQ-SRC-002", Severity.ERROR, "fail"),
        ]
    )
    assert "first: OQ-SRC-002" in str(error)


def test_bag_snapshot_does_not_alias_internal_list() -> None:
    bag = DiagnosticBag()
    bag.add(Diagnostic("OQ-SRC-001", Severity.ERROR, "one"))
    snapshot = bag.diagnostics
    bag.add(Diagnostic("OQ-SRC-002", Severity.ERROR, "two"))
    assert len(snapshot) == 1
    assert len(bag.diagnostics) == 2


def test_extend_adds_all() -> None:
    bag = DiagnosticBag()
    bag.extend(
        [
            Diagnostic("OQ-SRC-001", Severity.ERROR, "one"),
            Diagnostic("OQ-SRC-002", Severity.ERROR, "two"),
        ]
    )
    assert [d.code for d in bag] == ["OQ-SRC-001", "OQ-SRC-002"]
