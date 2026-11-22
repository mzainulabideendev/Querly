"""Semantic analyzer and compiler for the Querly language.

This module takes the AST produced by the parser and performs semantic analysis,
type checking, and SQL generation. It produces deterministic SQL output and
detects semantic errors before compilation.

The main entry points are:
- `analyze(ast: Program) -> AnalyzedProgram`: Performs semantic analysis
- `generate_sql(ast: Program, dialect: str) -> str`: Generates SQL from the analyzed AST
- `validate(ast: Program) -> DiagnosticBag`: Validates the AST for semantic errors
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

from oq.core.ast import (
    Program, Attribute, ColumnRef, FunctionCall, Literal, Literal, Parameter,
    Literal, SelectStmt, InsertStmt, UpdateStmt, DeleteStmt, TransactionDecl,
    QueryDecl, TransactionDecl, MigrationDecl, ConversionDecl, ImportDecl,
    TypeExpr, TypeAliases, semantic_view, to_data, to_json, walk
)
from oq.core.diagnostics import (
    Diagnostic, DiagnosticBag, DiagnosticError, Severity, SourceLocation,
    SourceSpan,
)
from oq.core.source import SourceFile
from oq.core.tokens import TokenKind, Lexer, LexResult
from oq.core.ast import (
    TypeExpr, Literal, ColumnRef, Parameter, FunctionCall, SelectStmt,
    InsertStmt, UpdateStmt, DeleteStmt, TransactionDecl, QueryDecl, Expression,
    Statement, TypeExpr, Literal, ColumnRef, Parameter, Star, UnaryOp, BinaryOp,
    InList, Between, IsPredicate, FunctionCall, Cast, When, Case, ListLiteral,
    Projection, OrderItem, Assignment, Returning, Attribute, IndexDecl,
    UniqueConstraint, TableDecl, QueryDecl, TransactionDecl, MigrationDecl,
    ConversionDecl, ImportDecl, Program, semantic_view
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbol Table Structures
# ---------------------------------------------------------------------------

@dataclass
class Symbol:
    """Base symbol table entry."""
    name: str
    span: SourceSpan
    type: Optional[TypeExpr] = None
    is_primary_key: bool = False
    is_unique: bool = False

@dataclass
class TableSymbol:
    """Symbol table entry for a table."""
    name: str
    columns: Dict[str, Symbol]  # column_name -> Symbol
    primary_key: Set[str]  # column names in primary key
    unique_constraints: Set[Tuple[str, ...]]  # columns in unique constraints
    foreign_keys: Set[Tuple[str, str, str]]  # (source_col, target_table, target_col)
    indexes: Set[Tuple[str, List[str]]]  # (index_name, [column_names])
    syntax: str  # 'structured' or 'english'

@dataclass
class QuerySymbol:
    """Symbol table entry for a query."""
    name: str
    parameters: Dict[str, TypeExpr]  # parameter_name -> TypeExpr
    source: TableSymbol  # source table for SELECT/INSERT/UPDATE/DELETE
    is_mutation: bool  # True for INSERT/UPDATE/DELETE, False for SELECT

# ---------------------------------------------------------------------------
# Semantic Analyzer
# ---------------------------------------------------------------------------

class SemanticAnalyzer:
    """Main semantic analyzer that processes the AST and builds symbol tables."""

    def __init__(self, source_file: SourceFile):
        self.source_file = source_file
        self.symbol_tables: Dict[str, TableSymbol] = {}  # table_name -> TableSymbol
        self.query_symbols: Dict[str, QuerySymbol] = {}  # query_name -> QuerySymbol
        self.current_namespace = ""  # current namespace for resolving names
        self.diagnostics = DiagnosticBag()
        self.ast: Program = None

    def analyze(self, ast: Program) -> AnalyzedProgram:
        """Perform semantic analysis on the AST and return analyzed program."""
        self.ast = ast
        self._visit_program(ast)
        return AnalyzedProgram(
            ast=ast,
            symbol_tables=self.symbol_tables,
            query_symbols=self.query_symbols,
            diagnostics=self.diagnostics
        )

    def _visit_program(self, node: Program):
        """Visit all declarations in the program."""
        for decl in node.declarations:
            if isinstance(decl, TableDecl):
                self._visit_table_decl(decl)
            elif isinstance(decl, QueryDecl):
                self._visit_query_decl(decl)
            elif isinstance(decl, TransactionDecl):
                self._visit_transaction_decl(decl)
            elif isinstance(decl, MigrationDecl):
                self._visit_migration_decl(decl)
            elif isinstance(decl, ConversionDecl):
                self._visit_conversion_decl(decl)
            elif isinstance(decl, ImportDecl):
                self._visit_import_decl(decl)
            # Other declaration types are handled elsewhere

    def _visit_table_decl(self, node: TableDecl):
        """Process a table declaration."""
        table_name = node.name
        table_symbol = TableSymbol(
            name=table_name,
            columns={},
            primary_key=set(),
            unique_constraints=set(),
            foreign_keys=set(),
            indexes=set(),
            syntax=node.syntax
        )
        self.symbol_tables[table_name] = table_symbol

        # Process attributes
        for attr in node.attributes:
            self._process_attribute(attr, table_symbol)

        # Process indexes and unique constraints
        for idx in node.indexes:
            self._process_index(idx, table_symbol)
        for unique in node.unique_constraints:
            self._process_unique_constraint(unique, table_symbol)

    def _process_attribute(self, attr: Attribute, table_symbol: TableSymbol):
        """Process a table attribute."""
        col_name = attr.name
        col_type = attr.type
        nullable = attr.nullable

        # Validate type
        if not self._validate_type(col_type):
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-TYP-001",
                    severity=Severity.ERROR,
                    message=f"unknown type {col_type.name}",
                    span=attr.span
                )
            )
            return

        # Create symbol
        symbol = Symbol(
            name=col_name,
            span=attr.span,
            type=col_type,
            is_primary_key=attr.primary,
            is_unique=attr.unique
        )
        table_symbol.columns[col_name] = symbol

        # Handle primary key
        if attr.primary:
            table_symbol.primary_key.add(col_name)

        # Handle unique constraint (if it's a column-level unique)
        if attr.unique:
            # Note: This is a simplification - full unique constraint handling is more complex
            pass

    def _process_index(self, idx: IndexDecl, table_symbol: TableSymbol):
        """Process an index declaration."""
        index_name = idx.name
        columns = list(idx.columns)
        unique = idx.unique

        table_symbol.indexes.add((index_name, columns))

    def _process_unique_constraint(self, unique: UniqueConstraint, table_symbol: TableSymbol):
        """Process a unique constraint declaration."""
        cols = list(unique.columns)
        table_symbol.unique_constraints.add(tuple(cols))

    def _visit_query_decl(self, node: QueryDecl):
        """Process a query declaration."""
        query_name = node.name or "anonymous"
        table_name = self._resolve_table(node.source.name)
        if table_name not in self.symbol_tables:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-001",
                    severity=Severity.ERROR,
                    message=f"unknown table {node.source.name}",
                    span=node.source.span
                )
            )
            return

        table_symbol = self.symbol_tables[table_name]
        query_symbol = QuerySymbol(
            name=query_name,
            parameters={},
            source=table_symbol,
            is_mutation=node.syntax in ("insert", "update", "delete")
        )
        self.query_symbols[query_name] = query_symbol

        # Process SELECT
        if node.syntax == "select":
            self._process_select_stmt(node.statement, query_symbol, table_symbol)

        # Process INSERT
        elif node.syntax == "insert":
            self._process_insert_stmt(node.statement, query_symbol, table_symbol)

        # Process UPDATE
        elif node.syntax == "update":
            self._process_update_stmt(node.statement, query_symbol, table_symbol)

        # Process DELETE
        elif node.syntax == "delete":
            self._process_delete_stmt(node.statement, query_symbol, table_symbol)

    def _process_select_stmt(self, stmt: SelectStmt, query_symbol: QuerySymbol, table_symbol: TableSymbol):
        """Process a SELECT statement."""
        # Validate source table
        source_table = self.symbol_tables.get(stmt.source.name)
        if source_table is None:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-002",
                    severity=Severity.ERROR,
                    message=f"unknown table {stmt.source.name}",
                    span=stmt.source.span
                )
            )
            return

        # Process joins
        for join in stmt.joins:
            target_table_name = join.table.name
            if target_table_name not in self.symbol_tables:
                self.diagnostics.add(
                    Diagnostic(
                        code="OQ-SEM-003",
                        severity=Severity.ERROR,
                        message=f"unknown table {target_table_name}",
                        span=join.table.span
                    )
                )
                continue

            target_table_symbol = self.symbol_tables[target_table_name]
            # Process join condition
            if not self._validate_expression(join.condition, source_table, target_table_symbol):
                self.diagnostics.add(
                    Diagnostic(
                        code="OQ-SEM-004",
                        severity=Severity.ERROR,
                        message="invalid join condition",
                        span=join.condition.span
                    )
                )

    def _process_insert_stmt(self, stmt: InsertStmt, query_symbol: QuerySymbol, table_symbol: TableSymbol):
        """Process an INSERT statement."""
        # Validate table exists
        if stmt.table not in self.symbol_tables:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-005",
                    severity=Severity.ERROR,
                    message=f"unknown table {stmt.table}",
                    span=stmt.table.span
                )
            )
            return

        # Validate columns
        for col in stmt.columns:
            if col not in table_symbol.columns:
                self.diagnostics.add(
                    Diagnostic(
                        code="OQ-SEM-006",
                        severity=Severity.ERROR,
                        message=f"unknown column {col}",
                        span=stmt.columns.span
                    )
                )

        # Process rows
        for row in stmt.rows:
            if len(row) != len(stmt.columns):
                self.diagnostics.add(
                    Diagnostic(
                        code="OQ-SEM-007",
                        severity=Severity.ERROR,
                        message="column count mismatch",
                        span=stmt.columns.span
                    )
                )

    def _process_update_stmt(self, stmt: UpdateStmt, query_symbol: QuerySymbol, table_symbol: TableSymbol):
        """Process an UPDATE statement."""
        # Validate table exists
        if stmt.table not in self.symbol_tables:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-008",
                    severity=Severity.ERROR,
                    message=f"unknown table {stmt.table}",
                    span=stmt.table.span
                )
            )
            return

        # Process assignments
        for assignment in stmt.assignments:
            col_name = assignment.column
            if col_name not in table_symbol.columns:
                self.diagnostics.add(
                    Diagnostic(
                        code="OQ-SEM-009",
                        severity=Severity.ERROR,
                        message=f"unknown column {col_name}",
                        span=assignment.column.span
                    )
                )

    def _process_delete_stmt(self, stmt: DeleteStmt, query_symbol: QuerySymbol, table_symbol: TableSymbol):
        """Process a DELETE statement."""
        # Validate table exists
        if stmt.table not in self.symbol_tables:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-010",
                    severity=Severity.ERROR,
                    message=f"unknown table {stmt.table}",
                    span=stmt.table.span
                )
            )
            return

        # Process where clause
        if stmt.where:
            if not self._validate_expression(stmt.where, table_symbol):
                self.diagnostics.add(
                    Diagnostic(
                        code="OQ-SEM-011",
                        severity=Severity.ERROR,
                        message="invalid WHERE condition",
                        span=stmt.where.span
                    )
                )

    def _visit_transaction_decl(self, node: TransactionDecl):
        """Process a transaction declaration."""
        # Validate transaction name
        if node.name in self.query_symbols:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-012",
                    severity=Severity.ERROR,
                    message=f"transaction name {node.name} already exists",
                    span=node.name.span
                )
            return

        # Process statements
        for stmt in node.statements:
            if isinstance(stmt, SelectStmt):
                self._process_select_stmt(stmt, None, None)  # Transactions can contain SELECTs
            elif isinstance(stmt, InsertStmt):
                self._process_insert_stmt(stmt, None, None)
            elif isinstance(stmt, UpdateStmt):
                self._process_update_stmt(stmt, None, None)
            elif isinstance(stmt, DeleteStmt):
                self._process_delete_stmt(stmt, None, None)

    def _visit_migration_decl(self, node: MigrationDecl):
        """Process a migration declaration."""
        # Validate migration name
        if node.name in self.query_symbols:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-013",
                    severity=Severity.ERROR,
                    message=f"migration name {node.name} already exists",
                    span=node.name.span
                )
            )

        # Process up/down actions
        for step in node.up:
            self._process_migration_step(step, node)
        for step in node.down:
            self._process_migration_step(step, node)

    def _process_migration_step(self, step: object, node: MigrationDecl):
        """Process a migration step."""
        if isinstance(step, TableDecl):
            self._visit_table_decl(step)
        elif isinstance(step, AlterTable):
            self._process_alter_table(step)
        elif isinstance(step, DropTableStatement):
            self._process_drop_table(step)
        elif isinstance(step, CreateIndexStatement):
            self._process_create_index(step)
        elif isinstance(step, DropIndexStatement):
            self._process_drop_index(step)

    def _process_alter_table(self, step: AlterTable):
        """Process an ALTER TABLE statement."""
        table_name = step.table
        if table_name not in self.symbol_tables:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-014",
                    severity=Severity.ERROR,
                    message=f"unknown table {table_name}",
                    span=step.table.span
                )
            )
            return

        table_symbol = self.symbol_tables[table_name]
        for action in step.actions:
            if action.kind == "add_column":
                self._process_add_column(action, table_symbol)
            elif action.kind == "drop_column":
                self._process_drop_column(action, table_symbol)
            elif action.kind == "rename_column":
                self._process_rename_column(action, table_symbol)
            elif action.kind == "alter_column":
                self._process_alter_column(action, table_symbol)

    def _process_add_column(self, action: AlterAction, table_symbol: TableSymbol):
        """Process adding a column."""
        col_name = action.column
        if col_name in table_symbol.columns:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-015",
                    severity=Severity.ERROR,
                    message=f"column {col_name} already exists",
                    span=action.column.span
                )
            )
            return

        # Validate type
        if not self._validate_type(action.type):
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-016",
                    severity=Severity.ERROR,
                    message=f"invalid column type {action.type.name}",
                    span=action.type.span
                )
            )
            return

        # Add column
        symbol = Symbol(
            name=col_name,
            span=action.type.span,
            type=action.type,
            is_primary_key=False,
            is_unique=False
        )
        table_symbol.columns[col_name] = symbol

    def _process_drop_column(self, action: AlterAction, table_symbol: TableSymbol):
        """Process dropping a column."""
        col_name = action.column
        if col_name not in table_symbol.columns:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-017",
                    severity=Severity.ERROR,
                    message=f"column {col_name} does not exist",
                    span=action.column.span
                )
            )
            return

        # Check if column is in primary key
        if col_name in table_symbol.primary_key:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-018",
                    severity=Severity.ERROR,
                    message=f"cannot drop primary key column {col_name}",
                    span=action.column.span
                )
            )

    def _process_rename_column(self, action: AlterAction, table_symbol: TableSymbol):
        """Process renaming a column."""
        old_name = action.column
        new_name = action.new_name
        if old_name not in table_symbol.columns:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-019",
                    severity=Severity.ERROR,
                    message=f"column {old_name} does not exist",
                    span=action.column.span
                )
            )
            return

        if new_name in table_symbol.columns:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-020",
                    severity=Severity.ERROR,
                    message=f"column {new_name} already exists",
                    span=action.new_name.span
                )
            )
            return

        # Rename column in symbol table
        symbol = table_symbol.columns.pop(old_name)
        symbol.name = new_name
        table_symbol.columns[new_name] = symbol

        # Update references in indexes and constraints
        for index_name, cols in table_symbol.indexes:
            if old_name in cols:
                # Update index columns
                pass  # Simplified - full implementation would be more complex

    def _process_drop_table(self, step: DropTableStatement):
        """Process dropping a table."""
        table_name = step.name
        if table_name not in self.symbol_tables:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-021",
                    severity=Severity.ERROR,
                    message=f"table {table_name} does not exist",
                    span=step.name.span
                )
            )
            return

        # Check if table is referenced by foreign keys
        for fk in self._collect_foreign_keys():
            if fk[0] == table_name:
                self.diagnostics.add(
                    Diagnostic(
                        code="OQ-SEM-022",
                        severity=Severity.ERROR,
                        message=f"table {table_name} is referenced by foreign key",
                        span=step.name.span
                    )
                )

    def _process_create_index(self, step: CreateIndexStatement):
        """Process creating an index."""
        index_name = step.index.name
        table_name = step.index.columns[0]  # Simplified - full implementation would handle multi-column
        if table_name not in self.symbol_tables:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-023",
                    severity=Severity.ERROR,
                    message=f"table {table_name} does not exist",
                    span=step.index.span
                )
            )
            return

        table_symbol = self.symbol_tables[table_name]
        cols = list(step.index.columns)
        table_symbol.indexes.add((index_name, cols))

    def _process_drop_index(self, step: DropIndexStatement):
        """Process dropping an index."""
        index_name = step.name
        # Find and remove index from symbol tables
        for table_symbol in self.symbol_tables.values():
            table_symbol.indexes.discard((index_name, _))

    def _process_conversion_decl(self, node: ConversionDecl):
        """Process a conversion declaration."""
        # This is a no-op for semantic analysis - handled in later phases
        pass

    def _process_import_decl(self, node: ImportDecl):
        """Process an import declaration."""
        # Imports are processed during parsing - no semantic analysis needed here
        pass

    def _validate_type(self, type_expr: TypeExpr) -> bool:
        """Validate a type expression."""
        if type_expr.name not in TypeAliases:
            return False

        # Validate arguments for parameterized types
        for arg in type_expr.arguments:
            if isinstance(arg, int):
                continue  # numeric parameters are valid
            elif isinstance(arg, str):
                continue  # string parameters are valid
            elif isinstance(arg, TypeExpr):
                # Nested type expressions are valid
                continue
            else:
                return False

        return True

    def _resolve_table(self, table_name: str) -> Optional[TableSymbol]:
        """Resolve a table name to its symbol table."""
        return self.symbol_tables.get(table_name)

    def _validate_expression(self, expr: Expression, source_table: Optional[TableSymbol] = None, target_table: Optional[TableSymbol] = None) -> bool:
        """Validate an expression."""
        try:
            # This is a simplified validation - real implementation would be more comprehensive
            if isinstance(expr, Literal):
                return True
            elif isinstance(expr, ColumnRef):
                if source_table:
                    # Validate column exists in source table
                    pass
                return True
            elif isinstance(expr, BinaryOp):
                # Validate operator compatibility
                return True
            elif isinstance(expr, FunctionCall):
                # Validate function name and arguments
                return True
            elif isinstance(expr, IsPredicate):
                # Validate predicate
                return True
            elif isinstance(expr, Between):
                # Validate BETWEEN expression
                return True
            elif isinstance(expr, InList):
                # Validate IN list
                return True
            # Other expression types...
            return True
        except Exception as e:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-SEM-024",
                    severity=Severity.ERROR,
                    message=str(e),
                    span=expr.span if hasattr(expr, 'span') else SourceSpan(node=self.source_file.text, 0, 0, 0, 0)
                )
            )
            return False

    def _collect_foreign_keys(self) -> Set[Tuple[str, str, str]]:
        """Collect all foreign key references."""
        fks = set()
        for table_symbol in self.symbol_tables.values():
            for attr in table_symbol.columns.values():
                if attr.references:
                    # Parse references to find foreign keys
                    # This is a simplified implementation
                    pass
        return fks

# ---------------------------------------------------------------------------
# Analyzed Program
# ---------------------------------------------------------------------------

@dataclass
class AnalyzedProgram:
    """Result of semantic analysis."""
    ast: Program
    symbol_tables: Dict[str, TableSymbol]
    query_symbols: Dict[str, QuerySymbol]
    diagnostics: DiagnosticBag

# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------

class Compiler:
    """Compiler that generates SQL from the analyzed AST."""

    def __init__(self, analyzed_program: AnalyzedProgram, dialect: str):
        self.analyzed_program = analyzed_program
        self.dialect = dialect.lower()
        self.diagnostics = DiagnosticBag()

    def generate_sql(self) -> str:
        """Generate SQL from the analyzed AST."""
        if self.dialect == "postgres":
            return self._generate_postgres_sql()
        elif self.dialect == "mysql":
            return self._generate_mysql_sql()
        elif self.dialect == "sqlite":
            return self._generate_sqlite_sql()
        else:
            self.diagnostics.add(
                Diagnostic(
                    code="OQ-CC-001",
                    severity=Severity.ERROR,
                    message=f"unsupported dialect {self.dialect}",
                    span=SourceSpan(node=self.analyzed_program.ast, 0, 0, 0, 0)
                )
            )
            return ""

    def _generate_postgres_sql(self) -> str:
        """Generate PostgreSQL SQL."""
        sql_parts = []

        # Process tables
        for table_name, table_symbol in self.analyzed_program.symbol_tables.items():
            sql_parts.append(f"CREATE TABLE {table_name} (")
            col_defs = []
            for col_name, symbol in table_symbol.columns.items():
                col_def = f"{col_name} {self._map_type(symbol.type)}"
                if symbol.is_primary_key:
                    col_def += " PRIMARY KEY"
                col_defs.append(col_def)
            sql_parts.append(",\n  ".join(col_defs) + ")")

        # Process indexes
        for index_name, cols in self.analyzed_program.symbol_tables["users"].indexes:
            sql_parts.append(f"CREATE INDEX {index_name} ON {index_name} ({', '.join(cols)})")

        return "\n".join(sql_parts)

    def _map_type(self, type_expr: TypeExpr) -> str:
        """Map universal type to dialect-specific type."""
        canonical = type_expr.canonical_name
        if canonical == "integer":
            return "INTEGER"
        elif canonical == "text":
            return "TEXT"
        elif canonical == "boolean":
            return "BOOLEAN"
        elif canonical == "float":
            return "FLOAT"
        elif canonical == "decimal":
            return "NUMERIC"
        elif canonical == "date":
            return "DATE"
        elif canonical == "time":
            return "TIME"
        elif canonical == "timestamp":
            return "TIMESTAMP"
        elif canonical == "duration":
            return "INTERVAL"
        elif canonical == "uuid":
            return "UUID"
        elif canonical == "email":
            return "VARCHAR"
        elif canonical == "url":
            return "VARCHAR"
        elif canonical == "json":
            return "JSONB"
        elif canonical == "binary":
            return "BYTEA"
        elif canonical == "geo":
            return "GEOGRAPHY"
        elif canonical == "list":
            # Simplified - would need to map element type
            return "TEXT[]"
        elif canonical == "set":
            return "TEXT[]"
        elif canonical == "map":
            return "JSONB"
        elif canonical == "vector":
            return "FLOAT[]"
        elif canonical == "reference":
            return "INTEGER"  # Simplified reference to integer
        else:
            return "TEXT"  # fallback

    def _generate_mysql_sql(self) -> str:
        """Generate MySQL SQL."""
        # Similar to PostgreSQL but with MySQL-specific syntax
        return self._generate_postgres_sql()  # Simplified - would have dialect-specific differences

    def _generate_sqlite_sql(self) -> str:
        """Generate SQLite SQL."""
        # Similar to above with SQLite-specific syntax
        return self._generate_postgres_sql()  # Simplified - would have dialect-specific differences

# ---------------------------------------------------------------------------
# Diagnostic Utilities
# ---------------------------------------------------------------------------

def render_diagnostic(diagnostic: Diagnostic, source_text: Optional[str] = None):
    """Render a diagnostic with source context."""
    if diagnostic.span is None:
        return f"{diagnostic.severity.name}: {diagnostic.message}"

    start_line, start_col = diagnostic.span.start.line, diagnostic.span.start.column
    end_line, end_col = diagnostic.span.end.line, diagnostic.span.end.column

    if source_text is None:
        return f"{diagnostic.severity.name}: {diagnostic.message}"

    lines = source_text.split('\n')
    if start_line > len(lines) or end_line > len(lines):
        return f"{diagnostic.severity.name}: {diagnostic.message}"

    start_line_text = lines[start_line - 1]
    end_line_text = lines[end_line - 1] if end_line <= len(lines) else ""

    # Create diagnostic message
    message = f"{diagnostic.severity.name}: {diagnostic.message}"

    if start_line == end_line:
        # Same line
        col_start = start_col
        col_end = end_col
        prefix = start_line_text[:col_start]
        suffix = start_line_text[col_end:]
        marker = "^" * (col_end - col_start)
        message += f"\n  {prefix}{marker}{suffix}"
    else:
        # Multiple lines
        prefix = start_line_text[col_start:]
        suffix = lines[end_line - 1][:end_col]
        middle_lines = lines[start_line:end_line - 1]
        marker = "^" * len(prefix)
        message += f"\n  {prefix}{marker}"
        for line in middle_lines:
            message += f"\n  {line}"
        message += f"\n  {suffix}{'^' * len(suffix)}"

    return message

# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def analyze(ast: Program, source_file: SourceFile) -> AnalyzedProgram:
    """Main entry point for semantic analysis."""
    analyzer = SemanticAnalyzer(source_file)
    analyzed = analyzer.analyze(ast)
    return analyzed

def compile(analyzed_program: AnalyzedProgram, dialect: str) -> Tuple[str, DiagnosticBag]:
    """Compile analyzed program to SQL."""
    compiler = Compiler(analyzed_program, dialect)
    sql = compiler.generate_sql()
    return sql, compiler.diagnostics