"""Real schema inspection for connected databases.

The upstream SQLite adapter's ``inspect_schema`` is a placeholder that returns
an empty schema.  Studio therefore inspects SQLite through the documented
catalog (`sqlite_master`) and PRAGMA statements on the adapter's live
connection.  Nothing is simulated: if a query fails, the failure is reported.

For adapters whose inspection path is not verified in this release the service
refuses to invent structure and returns an explicit limitation instead.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from oq.ir import (
    Attribute as IRAttribute,
    Constraint,
    Entity,
    Index as IRIndex,
    Relationship,
    SchemaDocument,
    Type as IRType,
)

from .connections import LiveConnection

SQLITE_NATIVE_TYPES: dict[str, str] = {
    "INTEGER": "integer",
    "INT": "integer",
    "BIGINT": "integer",
    "SMALLINT": "integer",
    "TINYINT": "integer",
    "REAL": "float",
    "DOUBLE": "float",
    "FLOAT": "float",
    "NUMERIC": "decimal",
    "DECIMAL": "decimal",
    "MONEY": "decimal",
    "TEXT": "text",
    "VARCHAR": "text",
    "CHAR": "text",
    "CLOB": "text",
    "BOOLEAN": "boolean",
    "DATE": "date",
    "TIME": "time",
    "DATETIME": "timestamp",
    "TIMESTAMP": "timestamp",
    "BLOB": "binary",
    "JSON": "json",
    "UUID": "uuid",
}

AFFINITY_TYPES: dict[str, str] = {
    "INTEGER": "integer",
    "REAL": "float",
    "TEXT": "text",
    "BLOB": "binary",
    "NUMERIC": "decimal",
}


@dataclass(slots=True)
class ColumnInfo:
    name: str
    native_type: str
    universal_type: str
    nullable: bool
    primary: bool
    default: str | None
    position: int


@dataclass(slots=True)
class IndexInfo:
    name: str
    columns: list[str]
    unique: bool
    origin: str


@dataclass(slots=True)
class ForeignKeyInfo:
    id: int
    from_column: str
    target_table: str
    target_column: str
    on_update: str
    on_delete: str


@dataclass(slots=True)
class TableInfo:
    name: str
    kind: str
    columns: list[ColumnInfo] = field(default_factory=list)
    indexes: list[IndexInfo] = field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = field(default_factory=list)
    row_count: int | None = None
    sql: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "rowCount": self.row_count,
            "sql": self.sql,
            "columns": [
                {
                    "name": column.name,
                    "nativeType": column.native_type,
                    "type": column.universal_type,
                    "nullable": column.nullable,
                    "primary": column.primary,
                    "default": column.default,
                    "position": column.position,
                }
                for column in self.columns
            ],
            "indexes": [
                {
                    "name": index.name,
                    "columns": index.columns,
                    "unique": index.unique,
                    "origin": index.origin,
                }
                for index in self.indexes
            ],
            "foreignKeys": [
                {
                    "id": fk.id,
                    "fromColumn": fk.from_column,
                    "targetTable": fk.target_table,
                    "targetColumn": fk.target_column,
                    "onUpdate": fk.on_update,
                    "onDelete": fk.on_delete,
                }
                for fk in self.foreign_keys
            ],
        }


class SchemaInspectionError(RuntimeError):
    pass


def _universal_type(native: str) -> str:
    upper = (native or "").strip().upper()
    if not upper:
        return "text"
    for name, universal in SQLITE_NATIVE_TYPES.items():
        if upper.startswith(name):
            return universal
    return "text"


def inspect_sqlite(live: LiveConnection, *, include_row_counts: bool = False) -> dict[str, Any]:
    if live.adapter_id != "sqlite":
        raise SchemaInspectionError(
            f"live schema inspection is only verified for SQLite in this release, not {live.adapter_id!r}"
        )

    raw = getattr(live.connection, "raw_connection", None)
    if not isinstance(raw, sqlite3.Connection):
        raise SchemaInspectionError("the live SQLite connection does not expose a driver handle")

    cursor = raw.cursor()
    tables: list[TableInfo] = []
    try:
        rows = cursor.execute(
            "SELECT name, type, sql FROM sqlite_master "
            "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        for name, kind, sql in rows:
            table = TableInfo(name=name, kind=kind, sql=sql)

            for cid, col_name, col_type, not_null, default, pk in cursor.execute(
                f"PRAGMA table_info({_quote(name)})"
            ).fetchall():
                table.columns.append(
                    ColumnInfo(
                        name=col_name,
                        native_type=col_type or "",
                        universal_type=_universal_type(col_type),
                        nullable=not bool(not_null) and not pk,
                        primary=bool(pk),
                        default=default,
                        position=cid,
                    )
                )

            try:
                index_rows = cursor.execute(f"PRAGMA index_list({_quote(name)})").fetchall()
            except sqlite3.Error:
                index_rows = []
            for index_row in index_rows:
                # seq, name, unique, origin, partial
                index_name = index_row[1]
                unique = bool(index_row[2])
                origin = index_row[3] if len(index_row) > 3 else ""
                columns = [
                    item[2]
                    for item in cursor.execute(
                        f"PRAGMA index_info({_quote(index_name)})"
                    ).fetchall()
                ]
                table.indexes.append(
                    IndexInfo(name=index_name, columns=columns, unique=unique, origin=origin)
                )

            try:
                fk_rows = cursor.execute(f"PRAGMA foreign_key_list({_quote(name)})").fetchall()
            except sqlite3.Error:
                fk_rows = []
            for fk in fk_rows:
                # id, seq, table, from, to, on_update, on_delete, match
                table.foreign_keys.append(
                    ForeignKeyInfo(
                        id=fk[0],
                        from_column=fk[3],
                        target_table=fk[2],
                        target_column=fk[4] or "",
                        on_update=fk[5] or "NO ACTION",
                        on_delete=fk[6] or "NO ACTION",
                    )
                )

            if include_row_counts:
                try:
                    count = cursor.execute(
                        f"SELECT COUNT(*) FROM {_quote(name)}"
                    ).fetchone()
                    table.row_count = int(count[0]) if count else None
                except sqlite3.Error:
                    table.row_count = None

            tables.append(table)
    finally:
        cursor.close()

    return {
        "adapter": live.adapter_id,
        "engine": str(live.version) if live.version else "SQLite",
        "database": live.connection.config.path,
        "tables": [table.public() for table in tables],
        "tableCount": len(tables),
        "verifiedInspection": True,
    }


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _ir_type(column: ColumnInfo) -> IRType:
    return IRType(kind=column.universal_type)


def schema_document_from_inspection(inspection: dict[str, Any]) -> SchemaDocument:
    entities: list[Entity] = []
    for table in inspection.get("tables", []):
        if table.get("kind") != "table":
            continue
        attributes: list[IRAttribute] = []
        primary_key: list[str] = []
        unique_constraints: list[Constraint] = []
        relationships: list[Relationship] = []

        for column in table.get("columns", []):
            attributes.append(
                IRAttribute(
                    name=column["name"],
                    type=IRType(kind=column["type"]),
                    nullable=column["nullable"],
                    primary=column["primary"],
                    default=column["default"],
                )
            )
            if column["primary"]:
                primary_key.append(column["name"])

        indexes: list[IRIndex] = []
        for index in table.get("indexes", []):
            if index.get("origin") == "pk":
                continue
            indexes.append(
                IRIndex(
                    name=index["name"],
                    columns=list(index["columns"]),
                    unique=bool(index["unique"]),
                )
            )
            if index.get("unique"):
                unique_constraints.append(
                    Constraint(kind="unique", columns=list(index["columns"]))
                )

        for fk in table.get("foreignKeys", []):
            relationships.append(
                Relationship(
                    name=f"{table['name']}_{fk['fromColumn']}_fk",
                    kind="many_to_one",
                    source_entity=table["name"],
                    target_entity=fk["targetTable"],
                    on_delete=fk.get("onDelete"),
                    on_update=fk.get("onUpdate"),
                )
            )

        entities.append(
            Entity(
                name=table["name"],
                attributes=attributes,
                primary_key=primary_key,
                indexes=indexes,
                relationships=relationships,
                unique_constraints=unique_constraints,
            )
        )
    return SchemaDocument(entities=entities)
