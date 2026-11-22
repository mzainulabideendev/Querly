"""Token definitions and the Querly lexer.

The lexer is hand-written so that it can implement rules a regular-expression
lexer cannot express safely:

* a run of line breaks is a single ``NEWLINE`` token (significant only in
  English-like declarations);
* ``2019-01-01`` is a date literal and ``2019 - 01 - 01`` is arithmetic, decided
  by maximal munch before the number rules run;
* a duration literal such as ``3d`` is one token, while ``3 days`` is not;
* every token carries an exact half-open :class:`~oq.core.diagnostics.SourceSpan`
  in Unicode code points.

The token kind names are the terminal names used by the Lark grammar in
``oq/core/grammar``, so there is exactly one definition of a token in the
project: this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from oq.core._text import LINE_BREAK_PATTERN
from oq.core.diagnostics import Diagnostic, DiagnosticBag, Severity, SourceSpan
from oq.core.source import SourceFile

__all__ = [
    "KEYWORDS",
    "LexResult",
    "Lexer",
    "Token",
    "TokenKind",
    "decode_string_literal",
]


class TokenKind(StrEnum):
    """Every kind of token the lexer can produce."""

    IDENT = "IDENT"
    QUOTED_IDENT = "QUOTED_IDENT"
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"
    FLOAT = "FLOAT"
    STRING = "STRING"
    DATE = "DATE"
    TIME = "TIME"
    TIMESTAMP = "TIMESTAMP"
    DURATION = "DURATION"
    NEWLINE = "NEWLINE"

    LBRACE = "LBRACE"
    RBRACE = "RBRACE"
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    LBRACKET = "LBRACKET"
    RBRACKET = "RBRACKET"
    COMMA = "COMMA"
    COLON = "COLON"
    DOT = "DOT"
    AT = "AT"
    DOLLAR = "DOLLAR"
    QUESTION = "QUESTION"
    STAR = "STAR"
    PLUS = "PLUS"
    MINUS = "MINUS"
    SLASH = "SLASH"
    PERCENT = "PERCENT"
    LT = "LT"
    LE = "LE"
    GT = "GT"
    GE = "GE"
    EQ = "EQ"
    NE = "NE"

    KW_IMPORT = "KW_IMPORT"
    KW_AS = "KW_AS"
    KW_TABLE = "KW_TABLE"
    KW_WITH = "KW_WITH"
    KW_CREATE = "KW_CREATE"
    KW_INDEX = "KW_INDEX"
    KW_ON = "KW_ON"
    KW_UNIQUE = "KW_UNIQUE"
    KW_MAKE = "KW_MAKE"
    KW_KEEP = "KW_KEEP"
    KW_REQUIRED = "KW_REQUIRED"
    KW_OPTIONAL = "KW_OPTIONAL"
    KW_AUTO = "KW_AUTO"
    KW_PRIMARY = "KW_PRIMARY"
    KW_KEY = "KW_KEY"
    KW_DEFAULT = "KW_DEFAULT"
    KW_CHECK = "KW_CHECK"
    KW_REFERENCES = "KW_REFERENCES"
    KW_DECIMAL = "KW_DECIMAL"
    KW_VECTOR = "KW_VECTOR"
    KW_LIST = "KW_LIST"
    KW_SET = "KW_SET"
    KW_MAP = "KW_MAP"
    KW_OF = "KW_OF"
    KW_TO = "KW_TO"
    KW_NUMBER = "KW_NUMBER"
    KW_INTEGER = "KW_INTEGER"
    KW_INT = "KW_INT"
    KW_FLOAT = "KW_FLOAT"
    KW_TEXT = "KW_TEXT"
    KW_STRING = "KW_STRING"
    KW_BOOLEAN = "KW_BOOLEAN"
    KW_BOOL = "KW_BOOL"
    KW_DATE = "KW_DATE"
    KW_TIME = "KW_TIME"
    KW_TIMESTAMP = "KW_TIMESTAMP"
    KW_DURATION = "KW_DURATION"
    KW_UUID = "KW_UUID"
    KW_EMAIL = "KW_EMAIL"
    KW_URL = "KW_URL"
    KW_JSON = "KW_JSON"
    KW_BINARY = "KW_BINARY"
    KW_GEO = "KW_GEO"
    KW_REFERENCE = "KW_REFERENCE"
    KW_QUERY = "KW_QUERY"
    KW_FROM = "KW_FROM"
    KW_SELECT = "KW_SELECT"
    KW_WHERE = "KW_WHERE"
    KW_GROUP = "KW_GROUP"
    KW_BY = "KW_BY"
    KW_HAVING = "KW_HAVING"
    KW_ORDER = "KW_ORDER"
    KW_ASC = "KW_ASC"
    KW_DESC = "KW_DESC"
    KW_NULLS = "KW_NULLS"
    KW_FIRST = "KW_FIRST"
    KW_LAST = "KW_LAST"
    KW_LIMIT = "KW_LIMIT"
    KW_OFFSET = "KW_OFFSET"
    KW_INSERT = "KW_INSERT"
    KW_INTO = "KW_INTO"
    KW_VALUES = "KW_VALUES"
    KW_UPDATE = "KW_UPDATE"
    KW_RETURNING = "KW_RETURNING"
    KW_DELETE = "KW_DELETE"
    KW_GET = "KW_GET"
    KW_ALL = "KW_ALL"
    KW_SORT = "KW_SORT"
    KW_ASCENDING = "KW_ASCENDING"
    KW_DESCENDING = "KW_DESCENDING"
    KW_TAKE = "KW_TAKE"
    KW_SKIP = "KW_SKIP"
    KW_ADD = "KW_ADD"
    KW_CHANGE = "KW_CHANGE"
    KW_REMOVE = "KW_REMOVE"
    KW_BEGIN = "KW_BEGIN"
    KW_TRANSACTION = "KW_TRANSACTION"
    KW_COMMIT = "KW_COMMIT"
    KW_MIGRATION = "KW_MIGRATION"
    KW_VERSION = "KW_VERSION"
    KW_ENTITY = "KW_ENTITY"
    KW_DESCRIPTION = "KW_DESCRIPTION"
    KW_UP = "KW_UP"
    KW_DOWN = "KW_DOWN"
    KW_ALTER = "KW_ALTER"
    KW_DROP = "KW_DROP"
    KW_COLUMN = "KW_COLUMN"
    KW_RENAME = "KW_RENAME"
    KW_TYPE = "KW_TYPE"
    KW_NOT = "KW_NOT"
    KW_NULL = "KW_NULL"
    KW_AND = "KW_AND"
    KW_OR = "KW_OR"
    KW_IN = "KW_IN"
    KW_BETWEEN = "KW_BETWEEN"
    KW_LIKE = "KW_LIKE"
    KW_IS = "KW_IS"
    KW_GREATER = "KW_GREATER"
    KW_LESS = "KW_LESS"
    KW_THAN = "KW_THAN"
    KW_AT = "KW_AT"
    KW_LEAST = "KW_LEAST"
    KW_MOST = "KW_MOST"
    KW_EQUAL = "KW_EQUAL"
    KW_ONE = "KW_ONE"
    KW_EMPTY = "KW_EMPTY"
    KW_TRUE = "KW_TRUE"
    KW_FALSE = "KW_FALSE"
    KW_CASE = "KW_CASE"
    KW_WHEN = "KW_WHEN"
    KW_THEN = "KW_THEN"
    KW_ELSE = "KW_ELSE"
    KW_END = "KW_END"
    KW_CAST = "KW_CAST"
    KW_INNER = "KW_INNER"
    KW_LEFT = "KW_LEFT"
    KW_RIGHT = "KW_RIGHT"
    KW_FULL = "KW_FULL"
    KW_JOIN = "KW_JOIN"
    KW_CONVERT = "KW_CONVERT"
    KW_SCHEMA = "KW_SCHEMA"
    KW_PROFILE = "KW_PROFILE"
    KW_ENTITIES = "KW_ENTITIES"
    KW_STRICT = "KW_STRICT"
    KW_COMPATIBLE = "KW_COMPATIBLE"
    KW_BEST_EFFORT = "KW_BEST_EFFORT"


KEYWORDS: Final[dict[str, TokenKind]] = {
    "as": TokenKind.KW_AS,
    "table": TokenKind.KW_TABLE,
    "with": TokenKind.KW_WITH,
    "create": TokenKind.KW_CREATE,
    "index": TokenKind.KW_INDEX,
    "on": TokenKind.KW_ON,
    "unique": TokenKind.KW_UNIQUE,
    "make": TokenKind.KW_MAKE,
    "keep": TokenKind.KW_KEEP,
    "required": TokenKind.KW_REQUIRED,
    "optional": TokenKind.KW_OPTIONAL,
    "auto": TokenKind.KW_AUTO,
    "primary": TokenKind.KW_PRIMARY,
    "key": TokenKind.KW_KEY,
    "default": TokenKind.KW_DEFAULT,
    "check": TokenKind.KW_CHECK,
    "references": TokenKind.KW_REFERENCES,
    "decimal": TokenKind.KW_DECIMAL,
    "vector": TokenKind.KW_VECTOR,
    "list": TokenKind.KW_LIST,
    "set": TokenKind.KW_SET,
    "map": TokenKind.KW_MAP,
    "of": TokenKind.KW_OF,
    "to": TokenKind.KW_TO,
    "number": TokenKind.KW_NUMBER,
    "integer": TokenKind.KW_INTEGER,
    "int": TokenKind.KW_INT,
    "float": TokenKind.KW_FLOAT,
    "text": TokenKind.KW_TEXT,
    "string": TokenKind.KW_STRING,
    "boolean": TokenKind.KW_BOOLEAN,
    "bool": TokenKind.KW_BOOL,
    "date": TokenKind.KW_DATE,
    "time": TokenKind.KW_TIME,
    "timestamp": TokenKind.KW_TIMESTAMP,
    "duration": TokenKind.KW_DURATION,
    "uuid": TokenKind.KW_UUID,
    "email": TokenKind.KW_EMAIL,
    "url": TokenKind.KW_URL,
    "json": TokenKind.KW_JSON,
    "binary": TokenKind.KW_BINARY,
    "geo": TokenKind.KW_GEO,
    "reference": TokenKind.KW_REFERENCE,
    "query": TokenKind.KW_QUERY,
    "from": TokenKind.KW_FROM,
    "select": TokenKind.KW_SELECT,
    "where": TokenKind.KW_WHERE,
    "group": TokenKind.KW_GROUP,
    "by": TokenKind.KW_BY,
    "having": TokenKind.KW_HAVING,
    "order": TokenKind.KW_ORDER,
    "asc": TokenKind.KW_ASC,
    "desc": TokenKind.KW_DESC,
    "nulls": TokenKind.KW_NULLS,
    "first": TokenKind.KW_FIRST,
    "last": TokenKind.KW_LAST,
    "limit": TokenKind.KW_LIMIT,
    "offset": TokenKind.KW_OFFSET,
    "insert": TokenKind.KW_INSERT,
    "into": TokenKind.KW_INTO,
    "values": TokenKind.KW_VALUES,
    "update": TokenKind.KW_UPDATE,
    "returning": TokenKind.KW_RETURNING,
    "delete": TokenKind.KW_DELETE,
    "get": TokenKind.KW_GET,
    "all": TokenKind.KW_ALL,
    "sort": TokenKind.KW_SORT,
    "ascending": TokenKind.KW_ASCENDING,
    "descending": TokenKind.KW_DESCENDING,
    "take": TokenKind.KW_TAKE,
    "skip": TokenKind.KW_SKIP,
    "add": TokenKind.KW_ADD,
    "change": TokenKind.KW_CHANGE,
    "remove": TokenKind.KW_REMOVE,
    "begin": TokenKind.KW_BEGIN,
    "transaction": TokenKind.KW_TRANSACTION,
    "commit": TokenKind.KW_COMMIT,
    "migration": TokenKind.KW_MIGRATION,
    "version": TokenKind.KW_VERSION,
    "entity": TokenKind.KW_ENTITY,
    "description": TokenKind.KW_DESCRIPTION,
    "up": TokenKind.KW_UP,
    "down": TokenKind.KW_DOWN,
    "alter": TokenKind.KW_ALTER,
    "drop": TokenKind.KW_DROP,
    "column": TokenKind.KW_COLUMN,
    "rename": TokenKind.KW_RENAME,
    "type": TokenKind.KW_TYPE,
    "not": TokenKind.KW_NOT,
    "null": TokenKind.KW_NULL,
    "and": TokenKind.KW_AND,
    "or": TokenKind.KW_OR,
    "in": TokenKind.KW_IN,
    "between": TokenKind.KW_BETWEEN,
    "like": TokenKind.KW_LIKE,
    "is": TokenKind.KW_IS,
    "greater": TokenKind.KW_GREATER,
    "less": TokenKind.KW_LESS,
    "than": TokenKind.KW_THAN,
    "at": TokenKind.KW_AT,
    "least": TokenKind.KW_LEAST,
    "most": TokenKind.KW_MOST,
    "equal": TokenKind.KW_EQUAL,
    "one": TokenKind.KW_ONE,
    "empty": TokenKind.KW_EMPTY,
    "true": TokenKind.KW_TRUE,
    "false": TokenKind.KW_FALSE,
    "case": TokenKind.KW_CASE,
    "when": TokenKind.KW_WHEN,
    "then": TokenKind.KW_THEN,
    "else": TokenKind.KW_ELSE,
    "end": TokenKind.KW_END,
    "cast": TokenKind.KW_CAST,
    "inner": TokenKind.KW_INNER,
    "left": TokenKind.KW_LEFT,
    "right": TokenKind.KW_RIGHT,
    "full": TokenKind.KW_FULL,
    "join": TokenKind.KW_JOIN,
    "convert": TokenKind.KW_CONVERT,
    "schema": TokenKind.KW_SCHEMA,
    "profile": TokenKind.KW_PROFILE,
    "entities": TokenKind.KW_ENTITIES,
    "strict": TokenKind.KW_STRICT,
    "compatible": TokenKind.KW_COMPATIBLE,
    "best-effort": TokenKind.KW_BEST_EFFORT,
}
"""Reserved structural and contextual keywords, matched case-insensitively."""


@dataclass(frozen=True, slots=True)
class Token:
    """A lexical token with its exact source span."""

    kind: TokenKind
    text: str
    span: SourceSpan

    @property
    def start(self) -> int:
        """Zero-based offset of the first code point of the token."""
        return self.span.start.offset

    @property
    def end(self) -> int:
        """Zero-based offset one past the last code point of the token."""
        return self.span.end.offset

    @property
    def line(self) -> int:
        """One-based line of the first code point."""
        return self.span.start.line

    @property
    def column(self) -> int:
        """One-based column of the first code point."""
        return self.span.start.column

    def is_keyword(self, keyword: str) -> bool:
        """Whether this token is the given keyword, case-insensitively."""
        return self.kind is KEYWORDS.get(keyword.lower())


@dataclass(frozen=True, slots=True)
class LexResult:
    """Tokens plus any lexical diagnostics.

    The token list never contains an ``INVALID`` token: characters that cannot
    start a token produce a diagnostic and are skipped, so parsing can continue
    with the surrounding structure.
    """

    tokens: tuple[Token, ...]
    diagnostics: tuple[Diagnostic, ...]

    @property
    def has_errors(self) -> bool:
        return any(item.is_error for item in self.diagnostics)


_PUNCTUATION: Final[dict[str, TokenKind]] = {
    "{": TokenKind.LBRACE,
    "}": TokenKind.RBRACE,
    "(": TokenKind.LPAREN,
    ")": TokenKind.RPAREN,
    "[": TokenKind.LBRACKET,
    "]": TokenKind.RBRACKET,
    ",": TokenKind.COMMA,
    ":": TokenKind.COLON,
    ".": TokenKind.DOT,
    "@": TokenKind.AT,
    "$": TokenKind.DOLLAR,
    "?": TokenKind.QUESTION,
    "*": TokenKind.STAR,
    "+": TokenKind.PLUS,
    "-": TokenKind.MINUS,
    "/": TokenKind.SLASH,
    "%": TokenKind.PERCENT,
    "<": TokenKind.LT,
    ">": TokenKind.GT,
    "=": TokenKind.EQ,
}

_TWO_CHAR: Final[dict[str, TokenKind]] = {
    "<=": TokenKind.LE,
    ">=": TokenKind.GE,
    "!=": TokenKind.NE,
    "<>": TokenKind.NE,
}

_TIMESTAMP_PATTERN: Final = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)
_DATE_PATTERN: Final = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_PATTERN: Final = re.compile(r"\d{2}:\d{2}:\d{2}")
_EXPONENT_PATTERN: Final = re.compile(r"[eE][+-]?\d+")
_DURATION_UNITS: Final[tuple[str, ...]] = ("min", "ms", "s", "h", "d", "w")

_SIMPLE_ESCAPES: Final[dict[str, str]] = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


def decode_string_literal(raw: str) -> str:
    """Decode a quoted string token, including its quotes, into its value.

    ``raw`` must include the surrounding double quotes. Invalid escapes raise
    :class:`ValueError`; the lexer reports them before this is called.
    """
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        msg = f"not a double-quoted string literal: {raw!r}"
        raise ValueError(msg)
    body = raw[1:-1]
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(body):
            msg = "trailing backslash in string literal"
            raise ValueError(msg)
        escape = body[index]
        index += 1
        if escape == "u":
            digits = body[index : index + 4]
            if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                msg = f"invalid unicode escape \\u{digits}"
                raise ValueError(msg)
            out.append(chr(int(digits, 16)))
            index += 4
            continue
        if escape not in _SIMPLE_ESCAPES:
            msg = f"invalid escape sequence \\{escape}"
            raise ValueError(msg)
        out.append(_SIMPLE_ESCAPES[escape])
    return "".join(out)


class Lexer:
    """Tokenize one :class:`~oq.core.source.SourceFile`."""

    def __init__(self, source: SourceFile) -> None:
        self.source = source
        self._text = source.text
        self._bag = DiagnosticBag()

    def tokenize(self) -> LexResult:
        """Produce every token in the file, including ``NEWLINE`` tokens."""
        text = self._text
        length = len(text)
        tokens: list[Token] = []
        index = 0

        while index < length:
            char = text[index]

            if char in " \t\f\v":
                index += 1
                continue

            if char in "\r\n":
                end = index
                while end < length:
                    match = LINE_BREAK_PATTERN.match(text, end)
                    if match is None:
                        break
                    end = match.end()
                    while end < length and text[end] in " \t":
                        end += 1
                tokens.append(self._token(TokenKind.NEWLINE, index, end))
                index = end
                continue

            if text.startswith("--", index):
                end = text.find("\n", index)
                cr = text.find("\r", index)
                candidates = [value for value in (end, cr) if value != -1]
                index = min(candidates) if candidates else length
                continue

            if text.startswith("/*", index):
                end = text.find("*/", index + 2)
                if end == -1:
                    self._error(
                        "OQ-LEX-004",
                        "unterminated block comment",
                        index,
                        length,
                        "a block comment must end with '*/'",
                    )
                    index = length
                else:
                    index = end + 2
                continue

            if char == '"':
                index = self._scan_string(index, tokens)
                continue

            if char == "`":
                index = self._scan_quoted_identifier(index, tokens)
                continue

            if char.isdigit():
                index = self._scan_number(index, tokens)
                continue

            if char == "_" or char.isidentifier():
                index = self._scan_identifier(index, tokens)
                continue

            two = text[index : index + 2]
            if two in _TWO_CHAR:
                tokens.append(self._token(_TWO_CHAR[two], index, index + 2))
                index += 2
                continue

            punctuation = _PUNCTUATION.get(char)
            if punctuation is not None:
                tokens.append(self._token(punctuation, index, index + 1))
                index += 1
                continue

            self._error(
                "OQ-LEX-001",
                f"unexpected character {char!r}",
                index,
                index + 1,
                "this character cannot start a Querly token",
            )
            index += 1

        return LexResult(tokens=tuple(tokens), diagnostics=self._bag.diagnostics)

    # -- scanners ---------------------------------------------------------

    def _scan_identifier(self, start: int, tokens: list[Token]) -> int:
        text = self._text
        if text.startswith("best-effort", start):
            after = start + len("best-effort")
            if after >= len(text) or not self._is_identifier_continue(text[after]):
                tokens.append(self._token(TokenKind.KW_BEST_EFFORT, start, after))
                return after
        index = start + 1
        while index < len(text):
            candidate = text[start : index + 1]
            if text[index] == "_" or candidate.isidentifier():
                index += 1
                continue
            break
        raw = text[start:index]
        kind = KEYWORDS.get(raw.lower(), TokenKind.IDENT)
        tokens.append(self._token(kind, start, index))
        return index

    def _scan_quoted_identifier(self, start: int, tokens: list[Token]) -> int:
        text = self._text
        index = start + 1
        while index < len(text):
            char = text[index]
            if char == "`":
                index += 1
                tokens.append(self._token(TokenKind.QUOTED_IDENT, start, index))
                return index
            if char in "\r\n":
                break
            index += 1
        self._error(
            "OQ-LEX-006",
            "unterminated quoted identifier",
            start,
            index,
            "a quoted identifier must end with a backtick on the same line",
        )
        return index

    def _scan_string(self, start: int, tokens: list[Token]) -> int:
        text = self._text
        index = start + 1
        closed = False
        while index < len(text):
            char = text[index]
            if char == '"':
                closed = True
                index += 1
                break
            if char in "\r\n":
                break
            if char == "\\":
                if index + 1 >= len(text):
                    break
                escape = text[index + 1]
                if escape == "u":
                    digits = text[index + 2 : index + 6]
                    if len(digits) == 4 and all(
                        c in "0123456789abcdefABCDEF" for c in digits
                    ):
                        index += 6
                        continue
                    self._error(
                        "OQ-LEX-003",
                        "invalid unicode escape sequence",
                        index,
                        min(index + 6, len(text)),
                        "unicode escapes use exactly four hexadecimal digits: \\uFFFF",
                    )
                    index += 2
                    continue
                if escape not in _SIMPLE_ESCAPES:
                    self._error(
                        "OQ-LEX-003",
                        f"invalid escape sequence \\{escape}",
                        index,
                        index + 2,
                        'valid escapes are \\" \\\\ \\/ \\b \\f \\n \\r \\t and \\uXXXX',
                    )
                index += 2
                continue
            index += 1

        if not closed:
            if index < len(text) and text[index] in "\r\n":
                self._error(
                    "OQ-LEX-007",
                    "line break inside string literal",
                    start,
                    index,
                    "string literals may not span lines; use \\n instead",
                )
            else:
                self._error(
                    "OQ-LEX-002",
                    "unterminated string literal",
                    start,
                    index,
                    'a string literal must end with a double quote on the same line',
                )
            return index
        tokens.append(self._token(TokenKind.STRING, start, index))
        return index

    def _scan_number(self, start: int, tokens: list[Token]) -> int:
        text = self._text

        for pattern, kind in (
            (_TIMESTAMP_PATTERN, TokenKind.TIMESTAMP),
            (_DATE_PATTERN, TokenKind.DATE),
            (_TIME_PATTERN, TokenKind.TIME),
        ):
            match = pattern.match(text, start)
            if match is not None:
                end = match.end()
                if end < len(text) and self._is_identifier_continue(text[end]):
                    break
                tokens.append(self._token(kind, start, end))
                return end

        index = start
        while index < len(text) and text[index].isdigit():
            index += 1
        kind = TokenKind.INTEGER
        if index + 1 < len(text) and text[index] == "." and text[index + 1].isdigit():
            index += 1
            while index < len(text) and text[index].isdigit():
                index += 1
            kind = TokenKind.DECIMAL
        exponent = _EXPONENT_PATTERN.match(text, index)
        if exponent is not None:
            index = exponent.end()
            kind = TokenKind.FLOAT

        for unit in _DURATION_UNITS:
            if text.startswith(unit, index):
                after = index + len(unit)
                if after >= len(text) or not self._is_identifier_continue(text[after]):
                    tokens.append(self._token(TokenKind.DURATION, start, after))
                    return after

        tokens.append(self._token(kind, start, index))
        return index

    # -- helpers ----------------------------------------------------------

    def _token(self, kind: TokenKind, start: int, end: int) -> Token:
        return Token(
            kind=kind,
            text=self._text[start:end],
            span=self.source.span(start, end),
        )

    def _error(
        self,
        code: str,
        message: str,
        start: int,
        end: int,
        note: str | None = None,
    ) -> None:
        self._bag.add(
            Diagnostic(
                code=code,
                severity=Severity.ERROR,
                message=message,
                span=self.source.span(start, end),
                notes=(note,) if note else (),
            )
        )

    @staticmethod
    def _is_identifier_continue(char: str) -> bool:
        return char == "_" or ("a" + char).isidentifier()
