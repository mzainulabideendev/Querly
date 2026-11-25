"""Recursive-descent parser for the Querly language subset used by Studio.

The parser consumes tokens from the upstream hand-written lexer
(``oq.core.tokens``) and produces the upstream immutable AST dataclasses
(``oq.core.ast``).  That keeps the Studio compiler on the same language model
as the CLI and SDK while avoiding a second token definition.

Supported syntax (both spellings where the language specification defines one):

* ``table`` declarations with attributes, modifiers, indexes and unique
  constraints;
* ``query`` declarations with select, insert, update and delete statements;
* anonymous structured mutations;
* English-like ``create table`` / ``get`` / ``add`` / ``change`` / ``remove``;
* conversion requests.

Anything outside the parsed subset produces an ``OQ-PARSE-*`` or ``OQ-SEM-*``
diagnostic.  The parser never invents a successful statement.
"""

from __future__ import annotations

from dataclasses import dataclass

from oq.core.ast import (
    Assignment,
    Attribute,
    Between,
    BinaryOp,
    Case,
    Cast,
    ColumnRef,
    ConversionDecl,
    DeleteStmt,
    Expression,
    FunctionCall,
    InList,
    IndexDecl,
    InsertStmt,
    IsPredicate,
    Join,
    ListLiteral,
    Literal,
    OrderItem,
    Parameter,
    Program,
    Projection,
    QueryDecl,
    Returning,
    SelectStmt,
    Star,
    Statement,
    TableDecl,
    TableRef,
    TypeExpr,
    UnaryOp,
    UniqueConstraint,
    UpdateStmt,
    When,
)
from oq.core.diagnostics import Diagnostic, DiagnosticBag, Severity, SourceSpan
from oq.core.source import SourceFile
from oq.core.tokens import Lexer, Token, TokenKind, decode_string_literal

SCALAR_TYPES = {
    TokenKind.KW_INTEGER: "integer",
    TokenKind.KW_INT: "integer",
    TokenKind.KW_NUMBER: "number",
    TokenKind.KW_FLOAT: "float",
    TokenKind.KW_DECIMAL: "decimal",
    TokenKind.KW_TEXT: "text",
    TokenKind.KW_STRING: "text",
    TokenKind.KW_BOOLEAN: "boolean",
    TokenKind.KW_BOOL: "boolean",
    TokenKind.KW_DATE: "date",
    TokenKind.KW_TIME: "time",
    TokenKind.KW_TIMESTAMP: "timestamp",
    TokenKind.KW_DURATION: "duration",
    TokenKind.KW_UUID: "uuid",
    TokenKind.KW_EMAIL: "email",
    TokenKind.KW_URL: "url",
    TokenKind.KW_JSON: "json",
    TokenKind.KW_BINARY: "binary",
    TokenKind.KW_GEO: "geo",
}

COLLECTION_TYPES = {
    TokenKind.KW_LIST: "list",
    TokenKind.KW_SET: "set",
    TokenKind.KW_MAP: "map",
    TokenKind.KW_VECTOR: "vector",
}

MODIFIER_START = {
    "primary",
    "required",
    "optional",
    "unique",
    "auto",
    "index",
    "default",
    "check",
    "references",
}

DECLARATION_START = {
    TokenKind.KW_TABLE,
    TokenKind.KW_QUERY,
    TokenKind.KW_INSERT,
    TokenKind.KW_UPDATE,
    TokenKind.KW_DELETE,
    TokenKind.KW_CREATE,
    TokenKind.KW_CONVERT,
    TokenKind.KW_IMPORT,
}

ENGLISH_DECLARATION_START = {
    TokenKind.KW_GET,
    TokenKind.KW_ADD,
    TokenKind.KW_CHANGE,
    TokenKind.KW_REMOVE,
    TokenKind.KW_CREATE,
    TokenKind.KW_BEGIN,
    TokenKind.KW_IMPORT,
    TokenKind.KW_CONVERT,
}

# Type names are contextual: ``email`` is both a type and a valid column name
# (the language specification's own schema example uses it as both).  The lexer
# classifies it as a keyword, so name positions accept these tokens explicitly.
NAME_KEYWORDS = (
    set(SCALAR_TYPES)
    | set(COLLECTION_TYPES)
    | {
        TokenKind.KW_REFERENCE,
        TokenKind.KW_DECIMAL,
        TokenKind.KW_KEY,
        TokenKind.KW_REQUIRED,
        TokenKind.KW_OPTIONAL,
        TokenKind.KW_UNIQUE,
        TokenKind.KW_AUTO,
        TokenKind.KW_PRIMARY,
        TokenKind.KW_INDEX,
        TokenKind.KW_DEFAULT,
        TokenKind.KW_CHECK,
        TokenKind.KW_REFERENCES,
    }
)


def _is_name_token(token: Token) -> bool:
    return token.kind in (TokenKind.IDENT, TokenKind.QUOTED_IDENT) or token.kind in NAME_KEYWORDS


@dataclass(slots=True)
class ParseResult:
    program: Program
    diagnostics: DiagnosticBag
    syntax: str


class ParseFailure(Exception):
    """Internal control-flow exception carrying a diagnostic."""


class Parser:
    def __init__(self, source: SourceFile) -> None:
        self.source = source
        result = Lexer(source).tokenize()
        self.diagnostics = DiagnosticBag(result.diagnostics)
        # Newlines are structural only for line-oriented English declarations.
        # Every token keeps its line, so the parser can use line boundaries
        # without needing a second token stream.
        self.tokens = tuple(token for token in result.tokens if token.kind is not TokenKind.NEWLINE)
        self.index = 0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def parse(self) -> ParseResult:
        declarations = []
        syntax_markers: set[str] = set()

        while not self.at_end:
            token = self.current
            try:
                if token.kind is TokenKind.KW_TABLE:
                    syntax_markers.add("structured")
                    declarations.append(self.parse_table())
                elif token.kind is TokenKind.KW_QUERY:
                    syntax_markers.add("structured")
                    declarations.append(self.parse_query_decl())
                elif token.kind in (
                    TokenKind.KW_INSERT,
                    TokenKind.KW_UPDATE,
                    TokenKind.KW_DELETE,
                ):
                    syntax_markers.add("structured")
                    statement = self.parse_statement()
                    declarations.append(
                        QueryDecl(
                            name=None,
                            parameters=(),
                            statement=statement,
                            syntax="structured",
                            span=statement.span,
                        )
                    )
                elif token.kind is TokenKind.KW_CREATE and self.peek(1).kind in (
                    TokenKind.KW_TABLE,
                    TokenKind.KW_INDEX,
                ):
                    syntax_markers.add("english")
                    if self.peek(1).kind is TokenKind.KW_TABLE:
                        declarations.append(self.parse_english_table())
                    else:
                        self.parse_english_index_top_level()
                        self._error_here(
                            "OQ-PARSE-030",
                            "top-level 'create index' is not supported in Studio yet",
                            "place the index inside a table declaration or a migration block",
                        )
                        self.recover_to_next_declaration()
                elif token.kind is TokenKind.KW_GET:
                    syntax_markers.add("english")
                    declarations.append(self.parse_english_select())
                elif token.kind is TokenKind.KW_ADD:
                    syntax_markers.add("english")
                    declarations.append(self.parse_english_insert())
                elif token.kind is TokenKind.KW_CHANGE:
                    syntax_markers.add("english")
                    declarations.append(self.parse_english_update())
                elif token.kind is TokenKind.KW_REMOVE:
                    syntax_markers.add("english")
                    declarations.append(self.parse_english_delete())
                elif token.kind is TokenKind.KW_CONVERT:
                    syntax_markers.add("structured")
                    declarations.append(self.parse_conversion())
                else:
                    self._error_here(
                        "OQ-PARSE-010",
                        f"unexpected token {token.text!r} at the start of a declaration",
                        "expected table, query, insert, update, delete, convert, or an English-like declaration",
                    )
                    self._advance()
                    self.recover_to_next_declaration()
            except ParseFailure:
                self.recover_to_next_declaration()

        syntax = "structured"
        if syntax_markers == {"english"}:
            syntax = "english"
        elif syntax_markers == {"english", "structured"}:
            syntax = "mixed"

        program = Program(declarations=tuple(declarations), span=self.source.span(0, self.source.length))
        return ParseResult(program=program, diagnostics=self.diagnostics, syntax=syntax)

    # ------------------------------------------------------------------
    # Token helpers
    # ------------------------------------------------------------------

    @property
    def current(self) -> Token:
        if self.index < len(self.tokens):
            return self.tokens[self.index]
        return self._eof_token()

    @property
    def at_end(self) -> bool:
        return self.index >= len(self.tokens)

    def peek(self, distance: int = 1) -> Token:
        index = self.index + distance
        if index < len(self.tokens):
            return self.tokens[index]
        return self._eof_token()

    def previous(self) -> Token | None:
        if self.index == 0:
            return None
        return self.tokens[self.index - 1]

    def _eof_token(self) -> Token:
        location = self.source.location_at(self.source.length)
        return Token(
            kind=TokenKind.IDENT,
            text="",
            span=SourceSpan(
                file=self.source.name,
                start=location,
                end=location,
            ),
        )

    def _advance(self) -> Token:
        token = self.current
        if self.index < len(self.tokens):
            self.index += 1
        return token

    def match(self, *kinds: TokenKind) -> Token | None:
        if self.current.kind in kinds:
            return self._advance()
        return None

    def match_keyword(self, *keywords: str) -> Token | None:
        for keyword in keywords:
            if self.current.is_keyword(keyword):
                return self._advance()
        return None

    def expect(self, kind: TokenKind, what: str | None = None) -> Token:
        if self.current.kind is kind:
            return self._advance()
        label = what or kind.value
        self._error_here(
            "OQ-PARSE-011",
            f"expected {label}, found {self.describe(self.current)}",
        )
        raise ParseFailure

    def expect_keyword(self, keyword: str) -> Token:
        if self.current.is_keyword(keyword):
            return self._advance()
        self._error_here(
            "OQ-PARSE-012",
            f"expected keyword {keyword!r}, found {self.describe(self.current)}",
        )
        raise ParseFailure

    def expect_name(self, what: str = "a name") -> Token:
        token = self.current
        if _is_name_token(token):
            return self._advance()
        # Keywords are not generally usable as identifiers.  The exception is
        # an engine column that happens to collide with a contextual keyword;
        # we still report it instead of silently accepting it.
        self._error_here("OQ-PARSE-013", f"expected {what}, found {self.describe(token)}")
        raise ParseFailure

    @staticmethod
    def name_of(token: Token) -> str:
        text = token.text
        if token.kind is TokenKind.QUOTED_IDENT and len(text) >= 2 and text[0] == "`":
            return text[1:-1]
        return text

    @staticmethod
    def describe(token: Token) -> str:
        if not token.text:
            return "end of file"
        return repr(token.text)

    def span_from(self, start: Token | int | None, end: Token | int | None = None) -> SourceSpan:
        start_offset = 0
        if isinstance(start, Token):
            start_offset = start.start
        elif isinstance(start, int):
            start_offset = start
        if isinstance(end, Token):
            end_offset = end.end
        elif isinstance(end, int):
            end_offset = end
        else:
            previous = self.previous()
            end_offset = previous.end if previous is not None else start_offset
        return self.source.span(start_offset, max(start_offset, end_offset))

    @property
    def current_line(self) -> int:
        return self.current.line if not self.at_end else self.source.line_count + 1

    def _error_here(self, code: str, message: str, note: str | None = None) -> None:
        token = self.current
        self.diagnostics.add(
            Diagnostic(
                code=code,
                severity=Severity.ERROR,
                message=message,
                span=self.source.span(token.start, max(token.end, token.start + 1)),
                notes=(note,) if note else (),
            )
        )

    def _error_at(self, token: Token, code: str, message: str, note: str | None = None) -> None:
        self.diagnostics.add(
            Diagnostic(
                code=code,
                severity=Severity.ERROR,
                message=message,
                span=self.source.span(token.start, max(token.end, token.start + 1)),
                notes=(note,) if note else (),
            )
        )

    def _warning_at(self, token: Token, code: str, message: str, note: str | None = None) -> None:
        self.diagnostics.add(
            Diagnostic(
                code=code,
                severity=Severity.WARNING,
                message=message,
                span=self.source.span(token.start, max(token.end, token.start + 1)),
                notes=(note,) if note else (),
            )
        )

    def recover_to_next_declaration(self) -> None:
        if self.at_end:
            return
        self._advance()
        while not self.at_end:
            if self.current.kind in DECLARATION_START:
                return
            if self.previous() is not None and self.current.line > self.previous().line:
                # English declarations are line-started; recover at a fresh line
                # if it begins with a declaration keyword.
                if self.current.kind in ENGLISH_DECLARATION_START:
                    return
            self._advance()

    # ------------------------------------------------------------------
    # Structured declarations
    # ------------------------------------------------------------------

    def parse_table(self) -> TableDecl:
        start = self.expect_keyword("table")
        name = self.name_of(self.expect_name("a table name"))
        self.expect(TokenKind.LBRACE, "'{'")
        attributes: list[Attribute] = []
        indexes: list[IndexDecl] = []
        uniques: list[UniqueConstraint] = []

        while not self.at_end and self.current.kind is not TokenKind.RBRACE:
            if self.current.kind is TokenKind.KW_INDEX:
                indexes.append(self.parse_index_decl())
            elif self.current.kind is TokenKind.KW_UNIQUE:
                uniques.append(self.parse_unique_decl())
            elif _is_name_token(self.current):
                attributes.append(self.parse_attribute())
            elif self.current.kind is TokenKind.COMMA:
                self._advance()
            else:
                self._error_here(
                    "OQ-PARSE-014",
                    f"unexpected token {self.describe(self.current)} in table body",
                    "expected an attribute, 'index', or 'unique'",
                )
                self._advance()
        self.expect(TokenKind.RBRACE, "'}'")
        end = self.previous()
        return TableDecl(
            name=name,
            attributes=tuple(attributes),
            indexes=tuple(indexes),
            unique_constraints=tuple(uniques),
            syntax="structured",
            span=self.span_from(start, end),
        )

    def parse_attribute(self) -> Attribute:
        start = self.expect_name("an attribute name")
        name = self.name_of(start)
        self.expect(TokenKind.COLON, "':'")
        type_expr = self.parse_type()
        nullable = False
        primary = False
        unique = False
        auto = False
        indexed = False
        default: Expression | None = None
        check: Expression | None = None
        references: str | None = None

        if self.match(TokenKind.QUESTION) is not None:
            nullable = True

        while self.current.kind is TokenKind.AT:
            self._advance()
            modifier = self.current
            if not _is_name_token(modifier):
                self._error_here("OQ-PARSE-013", "expected an attribute modifier after '@'")
                raise ParseFailure
            self._advance()
            key = modifier.text.lower()
            if key == "primary":
                primary = True
                self.match_keyword("key")
            elif key == "required":
                nullable = False
            elif key == "optional":
                nullable = True
            elif key == "unique":
                unique = True
            elif key == "auto":
                auto = True
            elif key == "index":
                indexed = True
            elif key == "default":
                self.expect(TokenKind.LPAREN, "'(' after @default")
                default = self.parse_expression()
                self.expect(TokenKind.RPAREN, "')'")
            elif key == "check":
                self.expect(TokenKind.LPAREN, "'(' after @check")
                check = self.parse_expression()
                self.expect(TokenKind.RPAREN, "')'")
            elif key == "references":
                if self.match(TokenKind.LPAREN) is not None:
                    references = self.parse_qualified_name()
                    self.expect(TokenKind.RPAREN, "')'")
                else:
                    references = self.parse_qualified_name()
            else:
                self._error_at(
                    modifier,
                    "OQ-PARSE-015",
                    f"unknown attribute modifier @{modifier.text}",
                )
        end = self.previous() or start
        return Attribute(
            name=name,
            type=type_expr,
            nullable=nullable,
            primary=primary,
            unique=unique,
            auto=auto,
            indexed=indexed,
            default=default,
            check=check,
            references=references,
            span=self.span_from(start, end),
        )

    def parse_type(self) -> TypeExpr:
        start = self.current
        if start.kind in SCALAR_TYPES:
            self._advance()
            name = SCALAR_TYPES[start.kind]
            arguments: tuple[TypeExpr | int | str, ...] = ()
            if name in ("text", "binary") and self.current.kind is TokenKind.LPAREN:
                self._advance()
                size = self.expect(TokenKind.INTEGER, "a length")
                arguments = (int(size.text),)
                self.expect(TokenKind.RPAREN, "')'")
            return TypeExpr(name=name, arguments=arguments, span=self.span_from(start, self.previous()))

        if start.kind is TokenKind.KW_DECIMAL:
            self._advance()
            arguments = ()
            if self.match(TokenKind.LPAREN) is not None:
                precision = self.expect(TokenKind.INTEGER, "a precision")
                scale = None
                if self.match(TokenKind.COMMA) is not None:
                    scale = self.expect(TokenKind.INTEGER, "a scale")
                self.expect(TokenKind.RPAREN, "')'")
                args: list[TypeExpr | int | str] = [int(precision.text)]
                if scale is not None:
                    args.append(int(scale.text))
                arguments = tuple(args)
            return TypeExpr(name="decimal", arguments=arguments, span=self.span_from(start, self.previous()))

        if start.kind is TokenKind.KW_VECTOR:
            self._advance()
            self.expect(TokenKind.LPAREN, "'(' after vector")
            dimension = self.expect(TokenKind.INTEGER, "a vector dimension")
            self.expect(TokenKind.RPAREN, "')'")
            return TypeExpr(
                name="vector",
                arguments=(int(dimension.text),),
                span=self.span_from(start, self.previous()),
            )

        if start.kind in (TokenKind.KW_LIST, TokenKind.KW_SET, TokenKind.KW_MAP):
            self._advance()
            name = "list" if start.kind is TokenKind.KW_LIST else "set" if start.kind is TokenKind.KW_SET else "map"
            if self.match(TokenKind.LT) is not None:
                first = self.parse_type()
                arguments: list[TypeExpr | int | str] = [first]
                if name == "map":
                    self.expect(TokenKind.COMMA, "',' in map type")
                    arguments.append(self.parse_type())
                self.expect(TokenKind.GT, "'>'")
                return TypeExpr(name=name, arguments=tuple(arguments), span=self.span_from(start, self.previous()))
            if self.match_keyword("of") is not None:
                first = self.parse_type()
                arguments = [first]
                if name == "map":
                    self.expect_keyword("to")
                    arguments.append(self.parse_type())
                return TypeExpr(name=name, arguments=tuple(arguments), span=self.span_from(start, self.previous()))
            self._error_at(start, "OQ-PARSE-016", f"expected element type after {name}")
            raise ParseFailure

        if start.kind is TokenKind.KW_REFERENCE:
            self._advance()
            target = ""
            if self.match(TokenKind.LPAREN) is not None:
                target = self.parse_qualified_name()
                self.expect(TokenKind.RPAREN, "')'")
            elif self.match_keyword("to") is not None:
                target = self.parse_qualified_name()
            return TypeExpr(
                name="reference",
                arguments=(target,),
                span=self.span_from(start, self.previous()),
            )

        self._error_at(
            start,
            "OQ-PARSE-017",
            f"expected a type, found {self.describe(start)}",
            "supported types include integer, text, decimal(p,s), timestamp, uuid, json, list<T>, map<K,V> and vector(n)",
        )
        raise ParseFailure

    def parse_index_decl(self) -> IndexDecl:
        start = self.expect_keyword("index")
        name = self.name_of(self.expect_name("an index name"))
        self.expect(TokenKind.LPAREN, "'('")
        columns = self.parse_name_list()
        self.expect(TokenKind.RPAREN, "')'")
        unique = self.match_keyword("unique") is not None
        return IndexDecl(name=name, columns=tuple(columns), unique=unique, span=self.span_from(start, self.previous()))

    def parse_unique_decl(self) -> UniqueConstraint:
        start = self.expect_keyword("unique")
        self.expect(TokenKind.LPAREN, "'('")
        columns = self.parse_name_list()
        self.expect(TokenKind.RPAREN, "')'")
        return UniqueConstraint(columns=tuple(columns), span=self.span_from(start, self.previous()))

    def parse_name_list(self) -> list[str]:
        names = [self.name_of(self.expect_name())]
        while self.match(TokenKind.COMMA) is not None:
            names.append(self.name_of(self.expect_name()))
        return names

    def parse_qualified_name(self) -> str:
        parts = [self.name_of(self.expect_name())]
        while self.match(TokenKind.DOT) is not None:
            parts.append(self.name_of(self.expect_name()))
        return ".".join(parts)

    def parse_query_decl(self) -> QueryDecl:
        start = self.expect_keyword("query")
        name: str | None = None
        if _is_name_token(self.current):
            name = self.name_of(self._advance())
        parameters: list[tuple[str, TypeExpr]] = []
        if self.match(TokenKind.LPAREN) is not None:
            while self.current.kind is not TokenKind.RPAREN and not self.at_end:
                pname = self.name_of(self.expect_name("a parameter name"))
                self.expect(TokenKind.COLON, "':'")
                parameters.append((pname, self.parse_type()))
                if self.match(TokenKind.COMMA) is None:
                    break
            self.expect(TokenKind.RPAREN, "')'")
        self.expect(TokenKind.LBRACE, "'{'")
        statement = self.parse_statement()
        self.expect(TokenKind.RBRACE, "'}'")
        return QueryDecl(
            name=name,
            parameters=tuple(parameters),
            statement=statement,
            syntax="structured",
            span=self.span_from(start, self.previous()),
        )

    # ------------------------------------------------------------------
    # Statements
    # ------------------------------------------------------------------

    def parse_statement(self) -> Statement:
        if self.current.kind is TokenKind.KW_SELECT or self.current.kind is TokenKind.KW_FROM:
            return self.parse_select()
        if self.current.kind is TokenKind.KW_INSERT:
            return self.parse_insert()
        if self.current.kind is TokenKind.KW_UPDATE:
            return self.parse_update()
        if self.current.kind is TokenKind.KW_DELETE:
            return self.parse_delete()
        self._error_here(
            "OQ-PARSE-018",
            f"expected a query statement, found {self.describe(self.current)}",
            "expected select, insert, update, or delete",
        )
        raise ParseFailure

    def parse_select(self) -> SelectStmt:
        start = self.current
        projections: tuple[Projection, ...] = ()
        source: TableRef | None = None

        if self.match_keyword("select") is not None:
            projections = self.parse_projections()
            self.expect_keyword("from")
            source = self.parse_table_ref()
        else:
            self.expect_keyword("from")
            source = self.parse_table_ref()
            self.expect_keyword("select")
            projections = self.parse_projections()

        joins: list[Join] = []
        while True:
            kind = "inner"
            if self.current.kind is TokenKind.KW_JOIN:
                kind = "inner"
            elif self.current.kind in (
                TokenKind.KW_INNER,
                TokenKind.KW_LEFT,
                TokenKind.KW_RIGHT,
                TokenKind.KW_FULL,
            ):
                kind = self.current.text.lower()
                self._advance()
                if kind != "inner":
                    self.match_keyword("outer")
            else:
                break
            self.expect_keyword("join")
            table = self.parse_table_ref()
            self.expect_keyword("on")
            condition = self.parse_expression()
            joins.append(Join(kind=kind, table=table, condition=condition, span=self.span_from(table.span.start.offset, self.previous())))

        where = None
        if self.match_keyword("where") is not None:
            where = self.parse_expression()

        group_by: list[Expression] = []
        if self.match_keyword("group") is not None:
            self.expect_keyword("by")
            group_by = self.parse_expression_list()

        having = None
        if self.match_keyword("having") is not None:
            having = self.parse_expression()

        order_by: list[OrderItem] = []
        if self.match_keyword("order") is not None:
            self.expect_keyword("by")
            order_by.append(self.parse_order_item())
            while self.match(TokenKind.COMMA) is not None:
                order_by.append(self.parse_order_item())

        limit = None
        offset = None
        if self.match_keyword("limit") is not None:
            limit = self.parse_integer("a limit")
        if self.match_keyword("offset") is not None:
            offset = self.parse_integer("an offset")

        return SelectStmt(
            source=source,
            joins=tuple(joins),
            projections=projections,
            where=where,
            group_by=tuple(group_by),
            having=having,
            order_by=tuple(order_by),
            limit=limit,
            offset=offset,
            span=self.span_from(start, self.previous()),
        )

    def parse_table_ref(self) -> TableRef:
        start = self.current
        table_start = start
        name = self.parse_qualified_name()
        alias = None
        if self.match_keyword("as") is not None:
            alias = self.name_of(self.expect_name("an alias"))
        elif (
            _is_name_token(self.current)
            and self.current.line == (self.previous().line if self.previous() else self.current.line)
        ):
            # Bare alias form: "from users u where ..."
            alias = self.name_of(self._advance())
        return TableRef(name=name, alias=alias, span=self.span_from(table_start, self.previous() or start))

    def parse_projections(self) -> tuple[Projection, ...]:
        items = [self.parse_projection()]
        while self.match(TokenKind.COMMA) is not None:
            items.append(self.parse_projection())
        return tuple(items)

    def parse_projection(self) -> Projection:
        start = self.current
        if self.current.kind is TokenKind.STAR:
            self._advance()
            expression: Expression = Star(span=self.span_from(start, self.previous()))
        else:
            expression = self.parse_expression()
        alias = None
        if self.match_keyword("as") is not None:
            alias = self.name_of(self.expect_name("an alias"))
        return Projection(expression=expression, alias=alias, span=self.span_from(start, self.previous()))

    def parse_order_item(self) -> OrderItem:
        start = self.current
        expression = self.parse_expression()
        direction = "ascending"
        if self.match_keyword("asc", "ascending") is not None:
            direction = "ascending"
        elif self.match_keyword("desc", "descending") is not None:
            direction = "descending"
        nulls = None
        if self.match_keyword("nulls") is not None:
            if self.match_keyword("first") is not None:
                nulls = "first"
            elif self.match_keyword("last") is not None:
                nulls = "last"
            else:
                self._error_here("OQ-PARSE-019", "expected 'first' or 'last' after 'nulls'")
        return OrderItem(expression=expression, direction=direction, nulls=nulls, span=self.span_from(start, self.previous()))

    def parse_integer(self, what: str) -> int:
        token = self.expect(TokenKind.INTEGER, what)
        try:
            return int(token.text)
        except ValueError:
            self._error_at(token, "OQ-PARSE-020", f"expected {what}")
            raise ParseFailure from None

    def parse_insert(self) -> InsertStmt:
        start = self.expect_keyword("insert")
        self.expect_keyword("into")
        table = self.parse_qualified_name()
        self.expect(TokenKind.LPAREN, "'('")
        columns = tuple(self.parse_name_list())
        self.expect(TokenKind.RPAREN, "')'")
        self.expect_keyword("values")
        rows: list[tuple[Expression, ...]] = []
        rows.append(self.parse_value_tuple())
        while self.match(TokenKind.COMMA) is not None:
            rows.append(self.parse_value_tuple())
        returning = None
        if self.match_keyword("returning") is not None:
            returning = Returning(projections=self.parse_projections(), span=self.span_from(start, self.previous()))
        return InsertStmt(
            table=table,
            columns=columns,
            rows=tuple(rows),
            returning=returning,
            span=self.span_from(start, self.previous()),
        )

    def parse_value_tuple(self) -> tuple[Expression, ...]:
        self.expect(TokenKind.LPAREN, "'('")
        values = [self.parse_expression()]
        while self.match(TokenKind.COMMA) is not None:
            values.append(self.parse_expression())
        self.expect(TokenKind.RPAREN, "')'")
        return tuple(values)

    def parse_update(self) -> UpdateStmt:
        start = self.expect_keyword("update")
        table = self.parse_qualified_name()
        self.expect_keyword("set")
        assignments = [self.parse_assignment()]
        while self.match(TokenKind.COMMA) is not None:
            assignments.append(self.parse_assignment())
        where = None
        if self.match_keyword("where") is not None:
            where = self.parse_expression()
        returning = None
        if self.match_keyword("returning") is not None:
            returning = Returning(projections=self.parse_projections(), span=self.span_from(start, self.previous()))
        return UpdateStmt(
            table=table,
            assignments=tuple(assignments),
            where=where,
            returning=returning,
            span=self.span_from(start, self.previous()),
        )

    def parse_assignment(self) -> Assignment:
        start = self.current
        column = self.name_of(self.expect_name("a column name"))
        if self.match(TokenKind.EQ) is None and self.match_keyword("to") is None:
            self._error_here("OQ-PARSE-021", "expected '=' or 'to' in assignment")
            raise ParseFailure
        value = self.parse_expression()
        return Assignment(column=column, value=value, span=self.span_from(start, self.previous()))

    def parse_delete(self) -> DeleteStmt:
        start = self.expect_keyword("delete")
        self.expect_keyword("from")
        table = self.parse_qualified_name()
        where = None
        if self.match_keyword("where") is not None:
            where = self.parse_expression()
        returning = None
        if self.match_keyword("returning") is not None:
            returning = Returning(projections=self.parse_projections(), span=self.span_from(start, self.previous()))
        return DeleteStmt(
            table=table,
            where=where,
            returning=returning,
            span=self.span_from(start, self.previous()),
        )

    def parse_conversion(self) -> ConversionDecl:
        start = self.expect_keyword("convert")
        self.expect_keyword("schema")
        self.expect_keyword("from")
        source_adapter = self.parse_qualified_name()
        self.expect_keyword("to")
        target_adapter = self.parse_qualified_name()
        self.expect(TokenKind.LBRACE, "'{'")
        self.expect_keyword("profile")
        profile_token = self.current
        if profile_token.kind not in (
            TokenKind.KW_STRICT,
            TokenKind.KW_COMPATIBLE,
            TokenKind.KW_BEST_EFFORT,
        ):
            self._error_here(
                "OQ-PARSE-022",
                "expected a conversion profile",
                "use strict, compatible, or best-effort",
            )
            raise ParseFailure
        self._advance()
        profile = profile_token.text.lower()
        entities: list[str] = []
        if self.match_keyword("entities") is not None:
            entities = self.parse_name_list()
        self.expect(TokenKind.RBRACE, "'}'")
        return ConversionDecl(
            source_adapter=source_adapter,
            target_adapter=target_adapter,
            profile=profile,
            entities=tuple(entities),
            span=self.span_from(start, self.previous()),
        )

    # ------------------------------------------------------------------
    # English-like declarations
    # ------------------------------------------------------------------

    def parse_english_table(self) -> TableDecl:
        start = self.expect_keyword("create")
        self.expect_keyword("table")
        name = self.name_of(self.expect_name("a table name"))
        self.match_keyword("with")
        self.expect(TokenKind.COLON, "':'")
        attributes: list[Attribute] = []
        indexes: list[IndexDecl] = []
        uniques: list[UniqueConstraint] = []

        while not self.at_end and self.current.kind not in ENGLISH_DECLARATION_START:
            if self.current.kind is TokenKind.KW_MAKE:
                indexes.append(self.parse_english_index())
            elif self.current.kind is TokenKind.KW_KEEP:
                uniques.append(self.parse_english_unique())
            elif _is_name_token(self.current):
                attributes.append(self.parse_english_field())
            elif self.current.kind is TokenKind.COMMA:
                self._advance()
            else:
                break
        end = self.previous() or start
        return TableDecl(
            name=name,
            attributes=tuple(attributes),
            indexes=tuple(indexes),
            unique_constraints=tuple(uniques),
            syntax="english",
            span=self.span_from(start, end),
        )

    def parse_english_field(self) -> Attribute:
        start = self.expect_name("a field name")
        name = self.name_of(start)
        self.expect_keyword("as")
        nullable = False
        primary = False
        unique = False
        auto = False
        indexed = False
        default = None
        check = None
        references = None
        type_expr: TypeExpr | None = None

        flags_seen: set[str] = set()
        while not self.at_end and self.current.line == start.line:
            token = self.current
            key = token.text.lower()
            if token.kind is TokenKind.KW_REQUIRED:
                self._advance()
                nullable = False
                flags_seen.add("nullability")
            elif token.kind is TokenKind.KW_OPTIONAL:
                self._advance()
                nullable = True
                flags_seen.add("nullability")
            elif token.kind is TokenKind.KW_UNIQUE:
                self._advance()
                unique = True
            elif token.kind is TokenKind.KW_AUTO:
                self._advance()
                auto = True
            elif token.kind is TokenKind.KW_PRIMARY:
                self._advance()
                primary = True
                self.match_keyword("key")
            elif token.kind is TokenKind.KW_INDEX:
                self._advance()
                indexed = True
            elif token.kind is TokenKind.KW_DEFAULT:
                self._advance()
                default = self.parse_expression()
            elif token.kind is TokenKind.KW_CHECK:
                self._advance()
                check = self.parse_expression()
            elif token.kind is TokenKind.KW_REFERENCES:
                self._advance()
                references = self.parse_qualified_name()
            elif (
                token.kind in SCALAR_TYPES
                or token.kind in COLLECTION_TYPES
                or token.kind is TokenKind.KW_REFERENCE
                or token.kind is TokenKind.KW_DECIMAL
            ) and type_expr is None:
                type_expr = self.parse_type()
            else:
                break
            if key in MODIFIER_START:
                flags_seen.add(key)

        if type_expr is None:
            self._error_at(
                start,
                "OQ-PARSE-023",
                f"field {name!r} is missing a type",
                "for example: name as required text",
            )
            raise ParseFailure
        end = self.previous() or start
        return Attribute(
            name=name,
            type=type_expr,
            nullable=nullable,
            primary=primary,
            unique=unique,
            auto=auto,
            indexed=indexed,
            default=default,
            check=check,
            references=references,
            span=self.span_from(start, end),
        )

    def parse_english_index(self) -> IndexDecl:
        start = self.expect_keyword("make")
        unique = self.match_keyword("unique") is not None
        self.expect_keyword("index")
        name = self.name_of(self.expect_name("an index name"))
        self.expect_keyword("on")
        self.expect(TokenKind.LPAREN, "'('")
        columns = self.parse_name_list()
        self.expect(TokenKind.RPAREN, "')'")
        return IndexDecl(name=name, columns=tuple(columns), unique=unique, span=self.span_from(start, self.previous()))

    def parse_english_unique(self) -> UniqueConstraint:
        start = self.expect_keyword("keep")
        self.expect(TokenKind.LPAREN, "'('")
        columns = self.parse_name_list()
        self.expect(TokenKind.RPAREN, "')'")
        self.expect_keyword("unique")
        return UniqueConstraint(columns=tuple(columns), span=self.span_from(start, self.previous()))

    def parse_english_select(self) -> QueryDecl:
        start = self.expect_keyword("get")
        projections: tuple[Projection, ...] = ()
        limit: int | None = None
        source: TableRef | None = None

        if self.match_keyword("all") is not None:
            name = self.parse_qualified_name()
            alias = None
            if self.match_keyword("as") is not None:
                alias = self.name_of(self.expect_name("an alias"))
            source = TableRef(name=name, alias=alias, span=self.span_from(start, self.previous()))
            projections = (Projection(expression=Star(span=self.span_from(start, self.previous())), alias=None, span=self.span_from(start, self.previous())),)
        elif self.match_keyword("first") is not None:
            limit = self.parse_integer("a row count")
            projections = self.parse_projections()
            self.expect_keyword("from")
            source = self.parse_table_ref()
        else:
            projections = self.parse_projections()
            self.expect_keyword("from")
            source = self.parse_table_ref()

        joins: list[Join] = []
        where = None
        group_by: list[Expression] = []
        having = None
        order_by: list[OrderItem] = []
        offset = None

        while not self.at_end:
            if self.current.kind in ENGLISH_DECLARATION_START:
                break
            if self.match_keyword("where") is not None:
                where = self.parse_expression()
            elif self.match_keyword("join") is not None:
                table = self.parse_table_ref()
                self.expect_keyword("on")
                condition = self.parse_expression()
                joins.append(Join(kind="inner", table=table, condition=condition, span=self.span_from(start, self.previous())))
            elif self.match_keyword("sort") is not None:
                self.expect_keyword("by")
                item = self.parse_english_order_item()
                order_by.append(item)
                while self.match(TokenKind.COMMA) is not None:
                    order_by.append(self.parse_english_order_item())
            elif self.match_keyword("take") is not None:
                limit = self.parse_integer("a row count")
            elif self.match_keyword("skip") is not None:
                offset = self.parse_integer("an offset")
            elif self.match_keyword("group") is not None:
                self.expect_keyword("by")
                group_by = self.parse_expression_list()
            elif self.match_keyword("having") is not None:
                having = self.parse_expression()
            else:
                break

        statement = SelectStmt(
            source=source,
            joins=tuple(joins),
            projections=projections,
            where=where,
            group_by=tuple(group_by),
            having=having,
            order_by=tuple(order_by),
            limit=limit,
            offset=offset,
            span=self.span_from(start, self.previous()),
        )
        return QueryDecl(name=None, parameters=(), statement=statement, syntax="english", span=statement.span)

    def parse_english_order_item(self) -> OrderItem:
        start = self.current
        expression = self.parse_expression()
        direction = "ascending"
        if self.match_keyword("ascending") is not None:
            direction = "ascending"
        elif self.match_keyword("descending") is not None:
            direction = "descending"
        return OrderItem(expression=expression, direction=direction, nulls=None, span=self.span_from(start, self.previous()))

    def parse_english_insert(self) -> QueryDecl:
        start = self.expect_keyword("add")
        self.expect_keyword("to")
        table = self.parse_qualified_name()
        self.expect(TokenKind.COLON, "':'")
        assignments = [self.parse_assignment()]
        while self.match(TokenKind.COMMA) is not None:
            assignments.append(self.parse_assignment())
        returning = None
        if self.match_keyword("returning") is not None:
            returning = Returning(projections=self.parse_projections(), span=self.span_from(start, self.previous()))
        statement = InsertStmt(
            table=table,
            columns=tuple(a.column for a in assignments),
            rows=(tuple(a.value for a in assignments),),
            returning=returning,
            span=self.span_from(start, self.previous()),
        )
        return QueryDecl(name=None, parameters=(), statement=statement, syntax="english", span=statement.span)

    def parse_english_update(self) -> QueryDecl:
        start = self.expect_keyword("change")
        table = self.parse_qualified_name()
        self.expect_keyword("set")
        assignments = [self.parse_assignment()]
        while self.match(TokenKind.COMMA) is not None:
            assignments.append(self.parse_assignment())
        where = None
        if self.match_keyword("where") is not None:
            where = self.parse_expression()
        statement = UpdateStmt(
            table=table,
            assignments=tuple(assignments),
            where=where,
            returning=None,
            span=self.span_from(start, self.previous()),
        )
        return QueryDecl(name=None, parameters=(), statement=statement, syntax="english", span=statement.span)

    def parse_english_delete(self) -> QueryDecl:
        start = self.expect_keyword("remove")
        self.expect_keyword("from")
        table = self.parse_qualified_name()
        where = None
        if self.match_keyword("where") is not None:
            where = self.parse_expression()
        statement = DeleteStmt(
            table=table,
            where=where,
            returning=None,
            span=self.span_from(start, self.previous()),
        )
        return QueryDecl(name=None, parameters=(), statement=statement, syntax="english", span=statement.span)

    def parse_english_index_top_level(self) -> None:
        self.expect_keyword("create")
        if self.current.kind is TokenKind.KW_INDEX:
            self.parse_index_decl()

    # ------------------------------------------------------------------
    # Expressions
    # ------------------------------------------------------------------

    def parse_expression_list(self) -> list[Expression]:
        items = [self.parse_expression()]
        while self.match(TokenKind.COMMA) is not None:
            items.append(self.parse_expression())
        return items

    def parse_expression(self) -> Expression:
        return self.parse_or()

    def parse_or(self) -> Expression:
        left = self.parse_and()
        while self.match_keyword("or") is not None:
            right = self.parse_and()
            left = BinaryOp(operator="or", left=left, right=right, span=self.span_from(left.span.start.offset, self.previous()))
        return left

    def parse_and(self) -> Expression:
        left = self.parse_not()
        while self.match_keyword("and") is not None:
            right = self.parse_not()
            left = BinaryOp(operator="and", left=left, right=right, span=self.span_from(left.span.start.offset, self.previous()))
        return left

    def parse_not(self) -> Expression:
        start = self.current
        if self.match_keyword("not") is not None:
            operand = self.parse_not()
            return UnaryOp(operator="not", operand=operand, span=self.span_from(start, self.previous()))
        return self.parse_comparison()

    def parse_comparison(self) -> Expression:
        left = self.parse_additive()
        token = self.current

        symbols = {
            TokenKind.LT: "<",
            TokenKind.LE: "<=",
            TokenKind.GT: ">",
            TokenKind.GE: ">=",
            TokenKind.EQ: "=",
            TokenKind.NE: "!=",
        }
        if token.kind in symbols:
            self._advance()
            right = self.parse_additive()
            return BinaryOp(operator=symbols[token.kind], left=left, right=right, span=self.span_from(left.span.start.offset, self.previous()))

        if self.match_keyword("like") is not None:
            right = self.parse_additive()
            return BinaryOp(operator="like", left=left, right=right, span=self.span_from(left.span.start.offset, self.previous()))

        if self.match_keyword("not") is not None:
            if self.match_keyword("like") is not None:
                right = self.parse_additive()
                return UnaryOp(
                    operator="not",
                    operand=BinaryOp(operator="like", left=left, right=right, span=self.span_from(left.span.start.offset, self.previous())),
                    span=self.span_from(left.span.start.offset, self.previous()),
                )
            if self.match_keyword("in") is not None:
                items = self.parse_in_list()
                return InList(expression=left, items=items, negated=True, span=self.span_from(left.span.start.offset, self.previous()))
            if self.match_keyword("between") is not None:
                low = self.parse_additive()
                self.expect_keyword("and")
                high = self.parse_additive()
                return Between(expression=left, low=low, high=high, negated=True, span=self.span_from(left.span.start.offset, self.previous()))
            self._error_here("OQ-PARSE-024", "expected like, in, or between after 'not'")
            raise ParseFailure

        if self.match_keyword("in") is not None:
            items = self.parse_in_list()
            return InList(expression=left, items=items, negated=False, span=self.span_from(left.span.start.offset, self.previous()))

        if self.match_keyword("between") is not None:
            low = self.parse_additive()
            self.expect_keyword("and")
            high = self.parse_additive()
            return Between(expression=left, low=low, high=high, negated=False, span=self.span_from(left.span.start.offset, self.previous()))

        if self.match_keyword("is") is not None:
            negated = self.match_keyword("not") is not None
            if self.match_keyword("null") is not None:
                return IsPredicate(expression=left, test="null", negated=negated, span=self.span_from(left.span.start.offset, self.previous()))
            if self.match_keyword("empty") is not None:
                return IsPredicate(expression=left, test="empty", negated=negated, span=self.span_from(left.span.start.offset, self.previous()))
            if self.match_keyword("greater") is not None:
                self.expect_keyword("than")
                right = self.parse_additive()
                operator = "<=" if negated else ">"
                return BinaryOp(operator=operator, left=left, right=right, span=self.span_from(left.span.start.offset, self.previous()))
            if self.match_keyword("less") is not None:
                self.expect_keyword("than")
                right = self.parse_additive()
                operator = ">=" if negated else "<"
                return BinaryOp(operator=operator, left=left, right=right, span=self.span_from(left.span.start.offset, self.previous()))
            if self.match_keyword("at") is not None:
                if self.match_keyword("least") is not None:
                    right = self.parse_additive()
                    operator = "<" if negated else ">="
                    return BinaryOp(operator=operator, left=left, right=right, span=self.span_from(left.span.start.offset, self.previous()))
                if self.match_keyword("most") is not None:
                    right = self.parse_additive()
                    operator = ">" if negated else "<="
                    return BinaryOp(operator=operator, left=left, right=right, span=self.span_from(left.span.start.offset, self.previous()))
                self._error_here("OQ-PARSE-025", "expected 'least' or 'most' after 'is at'")
                raise ParseFailure
            if self.match_keyword("equal") is not None:
                self.expect_keyword("to")
                right = self.parse_additive()
                operator = "!=" if negated else "="
                return BinaryOp(operator=operator, left=left, right=right, span=self.span_from(left.span.start.offset, self.previous()))
            if self.match_keyword("one") is not None:
                self.expect_keyword("of")
                items = self.parse_in_list()
                return InList(expression=left, items=items, negated=negated, span=self.span_from(left.span.start.offset, self.previous()))
            if self.match_keyword("like") is not None:
                right = self.parse_additive()
                return BinaryOp(operator="like", left=left, right=right, span=self.span_from(left.span.start.offset, self.previous()))
            if self.match_keyword("between") is not None:
                low = self.parse_additive()
                self.expect_keyword("and")
                high = self.parse_additive()
                return Between(expression=left, low=low, high=high, negated=negated, span=self.span_from(left.span.start.offset, self.previous()))
            self._error_here(
                "OQ-PARSE-026",
                "unsupported 'is' comparison",
                "use is null, is empty, is greater than, is less than, is at least, is at most, is equal to, or is one of",
            )
            raise ParseFailure

        return left

    def parse_in_list(self) -> tuple[Expression, ...]:
        self.expect(TokenKind.LPAREN, "'('")
        items = [self.parse_expression()]
        while self.match(TokenKind.COMMA) is not None:
            items.append(self.parse_expression())
        self.expect(TokenKind.RPAREN, "')'")
        return tuple(items)

    def parse_additive(self) -> Expression:
        left = self.parse_multiplicative()
        while self.current.kind in (TokenKind.PLUS, TokenKind.MINUS):
            operator = "+" if self._advance().kind is TokenKind.PLUS else "-"
            right = self.parse_multiplicative()
            left = BinaryOp(operator=operator, left=left, right=right, span=self.span_from(left.span.start.offset, self.previous()))
        return left

    def parse_multiplicative(self) -> Expression:
        left = self.parse_unary()
        while self.current.kind in (TokenKind.STAR, TokenKind.SLASH, TokenKind.PERCENT):
            token = self._advance()
            operator = {TokenKind.STAR: "*", TokenKind.SLASH: "/", TokenKind.PERCENT: "%"}[token.kind]
            right = self.parse_unary()
            left = BinaryOp(operator=operator, left=left, right=right, span=self.span_from(left.span.start.offset, self.previous()))
        return left

    def parse_unary(self) -> Expression:
        start = self.current
        if self.current.kind in (TokenKind.PLUS, TokenKind.MINUS):
            operator = "+" if self._advance().kind is TokenKind.PLUS else "-"
            operand = self.parse_unary()
            return UnaryOp(operator=operator, operand=operand, span=self.span_from(start, self.previous()))
        return self.parse_postfix()

    def parse_postfix(self) -> Expression:
        expression = self.parse_primary()
        while True:
            if self.match(TokenKind.DOT) is not None:
                atom = self.expect_name("a field name")
                parts = expression.parts if isinstance(expression, ColumnRef) else ()
                expression = ColumnRef(parts=(*parts, self.name_of(atom)), span=self.span_from(expression.span.start.offset, self.previous()))
            elif self.match(TokenKind.LBRACKET) is not None:
                self.parse_expression()
                self.expect(TokenKind.RBRACKET, "']'")
                self._error_here(
                    "OQ-PARSE-027",
                    "index access is parsed but not compiled by Studio yet",
                    "remove the index access or use a supported function",
                )
                raise ParseFailure
            else:
                break
        return expression

    def parse_primary(self) -> Expression:
        token = self.current
        start = token

        if token.kind is TokenKind.INTEGER:
            self._advance()
            return Literal(kind="integer", value=int(token.text), span=self.span_from(token))
        if token.kind in (TokenKind.DECIMAL, TokenKind.FLOAT):
            self._advance()
            try:
                value = float(token.text)
            except ValueError:
                self._error_at(token, "OQ-PARSE-028", f"invalid numeric literal {token.text!r}")
                raise ParseFailure from None
            kind = "float" if token.kind is TokenKind.FLOAT else "decimal"
            return Literal(kind=kind, value=token.text, span=self.span_from(token))
        if token.kind is TokenKind.STRING:
            self._advance()
            try:
                value = decode_string_literal(token.text)
            except ValueError as exc:
                self._error_at(token, "OQ-PARSE-029", str(exc))
                raise ParseFailure from None
            return Literal(kind="string", value=value, span=self.span_from(token))
        if token.kind in (
            TokenKind.DATE,
            TokenKind.TIME,
            TokenKind.TIMESTAMP,
            TokenKind.DURATION,
        ):
            self._advance()
            kind = {
                TokenKind.DATE: "date",
                TokenKind.TIME: "time",
                TokenKind.TIMESTAMP: "timestamp",
                TokenKind.DURATION: "duration",
            }[token.kind]
            return Literal(kind=kind, value=token.text, span=self.span_from(token))
        if token.kind in (TokenKind.KW_TRUE, TokenKind.KW_FALSE):
            self._advance()
            return Literal(kind="boolean", value=token.kind is TokenKind.KW_TRUE, span=self.span_from(token))
        if token.kind is TokenKind.KW_NULL:
            self._advance()
            return Literal(kind="null", value=None, span=self.span_from(token))
        if token.kind is TokenKind.DOLLAR:
            self._advance()
            name = self.name_of(self.expect_name("a parameter name"))
            return Parameter(name=name, span=self.span_from(start, self.previous()))
        if token.kind is TokenKind.LPAREN:
            self._advance()
            expression = self.parse_expression()
            self.expect(TokenKind.RPAREN, "')'")
            return expression
        if token.kind is TokenKind.LBRACKET:
            self._advance()
            items: list[Expression] = []
            if self.current.kind is not TokenKind.RBRACKET:
                items = self.parse_expression_list()
            self.expect(TokenKind.RBRACKET, "']'")
            return ListLiteral(items=tuple(items), span=self.span_from(token, self.previous()))
        if token.kind is TokenKind.KW_CAST:
            self._advance()
            self.expect(TokenKind.LPAREN, "'('")
            expression = self.parse_expression()
            self.expect_keyword("as")
            target = self.parse_type()
            self.expect(TokenKind.RPAREN, "')'")
            return Cast(expression=expression, target=target, span=self.span_from(token, self.previous()))
        if token.kind is TokenKind.KW_CASE:
            return self.parse_case()
        if token.kind is TokenKind.STAR:
            self._advance()
            return Star(span=self.span_from(token))
        if _is_name_token(token):
            self._advance()
            parts = [self.name_of(token)]
            while self.match(TokenKind.DOT) is not None:
                parts.append(self.name_of(self.expect_name("a name")))
            if self.match(TokenKind.LPAREN) is not None:
                arguments: list[Expression] = []
                if self.current.kind is not TokenKind.RPAREN:
                    if self.match(TokenKind.STAR) is not None:
                        arguments.append(Star(span=self.span_from(self.previous())))
                    else:
                        arguments = self.parse_expression_list()
                self.expect(TokenKind.RPAREN, "')'")
                return FunctionCall(name=tuple(parts), arguments=tuple(arguments), span=self.span_from(token, self.previous()))
            return ColumnRef(parts=tuple(parts), span=self.span_from(token, self.previous()))

        self._error_here(
            "OQ-PARSE-031",
            f"expected an expression, found {self.describe(token)}",
        )
        raise ParseFailure

    def parse_case(self) -> Case:
        start = self.expect_keyword("case")
        whens: list[When] = []
        while self.match_keyword("when") is not None:
            condition = self.parse_expression()
            self.expect_keyword("then")
            result = self.parse_expression()
            whens.append(When(condition=condition, result=result, span=self.span_from(condition.span.start.offset, self.previous())))
        otherwise = None
        if self.match_keyword("else") is not None:
            otherwise = self.parse_expression()
        self.expect_keyword("end")
        if not whens:
            self._error_here("OQ-PARSE-032", "case expression requires at least one when clause")
            raise ParseFailure
        return Case(whens=tuple(whens), otherwise=otherwise, span=self.span_from(start, self.previous()))


def parse_source(source: SourceFile | str, *, path: str = "<memory>") -> ParseResult:
    """Parse a source string or :class:`SourceFile`."""

    if isinstance(source, str):
        source = SourceFile.from_text(source, name=path)
    return Parser(source).parse()
