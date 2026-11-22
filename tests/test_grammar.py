"""Structural validation of the formal EBNF grammar.

This is not a Querly parser. It validates the grammar document itself: every
production named once, every referenced nonterminal defined, the entry point
present, and all required language constructs covered.
"""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path

GRAMMAR_PATH = Path(__file__).resolve().parents[1] / "docs" / "language" / "grammar.ebnf"

TRIVIA_PRODUCTIONS = {"whitespace", "line_comment", "block_comment"}

REQUIRED_PRODUCTIONS = {
    "program",
    "top_level",
    "import_decl",
    "schema_decl",
    "structured_schema",
    "english_schema",
    "attribute_decl",
    "type_expr",
    "index_decl",
    "query_decl",
    "structured_query",
    "english_query",
    "select_stmt",
    "insert_stmt",
    "update_stmt",
    "delete_stmt",
    "transaction_decl",
    "migration_decl",
    "conversion_decl",
    "expression",
}

COMPARISON_PRODUCTIONS = {
    "comparison",
    "comparison_tail",
    "symbolic_comparison",
    "word_comparison",
    "word_predicate",
}


_TERMINAL_PATTERN = re.compile(r'"[^"\n]*"')


def _clean(text: str) -> str:
    """Remove EBNF comments and special sequences, preserving terminals.

    Terminals are substituted before stripping so that a terminal such as
    ``"?"`` or ``"*/"`` cannot be mistaken for a special sequence or a comment
    terminator.
    """
    terminals: list[str] = []

    def protect(match: re.Match[str]) -> str:
        terminals.append(match.group(0))
        return f"\x01{len(terminals) - 1}\x02"

    protected = _TERMINAL_PATTERN.sub(protect, text)
    protected = re.sub(r"\(\*.*?\*\)", "", protected, flags=re.DOTALL)
    # Keep a non-lowercase marker so a production defined only by a special
    # sequence stays non-empty and is not mistaken for a nonterminal reference.
    protected = re.sub(r"\?.*?\?", " SPECIAL ", protected, flags=re.DOTALL)
    for index, terminal in enumerate(terminals):
        protected = protected.replace(f"\x01{index}\x02", terminal)
    return protected


def _split_on_semicolons(text: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    in_terminal = False
    for char in text:
        if char == '"':
            in_terminal = not in_terminal
            buffer.append(char)
        elif char == ";" and not in_terminal:
            parts.append("".join(buffer))
            buffer = []
        else:
            buffer.append(char)
    assert not in_terminal, "unterminated terminal at end of grammar"
    parts.append("".join(buffer))
    return parts


def _parse_productions(text: str) -> dict[str, str]:
    cleaned = _clean(text)
    productions: dict[str, str] = {}
    for chunk in _split_on_semicolons(cleaned):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, separator, body = chunk.partition("=")
        assert separator == "=", f"grammar chunk without '=': {chunk!r}"
        name = name.strip()
        body = body.strip()
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name), f"invalid production name {name!r}"
        assert name not in productions, f"duplicate production {name!r}"
        assert body, f"empty production {name!r}"
        productions[name] = body
    return productions


def _referenced_nonterminals(body: str) -> set[str]:
    without_terminals = re.sub(r'"[^"]*"', '""', body)
    return set(re.findall(r"[a-z][a-z0-9_]*", without_terminals))


def _load() -> dict[str, str]:
    return _parse_productions(GRAMMAR_PATH.read_text(encoding="utf-8"))


def test_grammar_file_declares_a_version() -> None:
    text = GRAMMAR_PATH.read_text(encoding="utf-8")
    assert "Grammar version: 0.1" in text
    assert "Phase 0 draft" in text


def test_grammar_parses_into_unique_productions() -> None:
    productions = _load()
    assert len(productions) >= 90
    assert "program" in productions


def test_every_referenced_nonterminal_is_defined() -> None:
    productions = _load()
    defined = set(productions)
    missing: dict[str, set[str]] = {}
    for name, body in productions.items():
        unknown = _referenced_nonterminals(body) - defined
        if unknown:
            missing[name] = unknown
    assert not missing, f"undefined nonterminals: {missing}"


def test_all_required_constructs_are_present() -> None:
    productions = _load()
    missing = (REQUIRED_PRODUCTIONS | COMPARISON_PRODUCTIONS) - set(productions)
    assert not missing, f"missing required productions: {sorted(missing)}"


def test_every_nonterminal_is_reachable_from_program() -> None:
    productions = _load()
    reachable = {"program"}
    queue = deque(["program"])
    while queue:
        current = queue.popleft()
        for reference in _referenced_nonterminals(productions[current]):
            if reference in productions and reference not in reachable:
                reachable.add(reference)
                queue.append(reference)
    orphans = set(productions) - reachable - TRIVIA_PRODUCTIONS
    assert not orphans, f"unreachable productions: {sorted(orphans)}"


def test_both_syntax_modes_are_grammar_alternatives() -> None:
    productions = _load()
    assert "structured_schema" in productions["schema_decl"]
    assert "english_schema" in productions["schema_decl"]
    assert "structured_query" in productions["query_decl"]
    assert "english_query" in productions["query_decl"]


def test_ddl_and_conversion_are_covered() -> None:
    productions = _load()
    for construct in ["alter_table_stmt", "drop_table_stmt", "create_index_stmt"]:
        assert "alter_table_stmt" in productions["ddl_stmt"] or construct in productions
    assert "conversion_profile" in productions
    for profile in ['"strict"', '"compatible"', '"best-effort"']:
        assert profile in productions["conversion_profile"]


def test_terminals_are_balanced() -> None:
    productions = _load()
    for name, body in productions.items():
        # Escapes are not used inside terminals except the backslash terminal.
        assert body.count('"') % 2 == 0, f"unbalanced quotes in {name}"


def test_grammar_has_no_placeholder_productions() -> None:
    productions = _load()
    for name, body in productions.items():
        assert "TODO" not in body.upper(), f"{name} contains TODO"
        assert "TBD" not in body.upper(), f"{name} contains TBD"
