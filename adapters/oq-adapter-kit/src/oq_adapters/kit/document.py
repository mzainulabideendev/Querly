"""Helpers for document, search, vector, and wide-column adapters."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from oq.ir import AdapterDiagnostic, DocumentQuery, PlanDocument, Safety
from oq.runtime.contract import AdapterErrorCode
from oq_adapters.kit.common import diagnostic

READ_OPERATIONS = {
    "find",
    "find_one",
    "aggregate",
    "count",
    "distinct",
    "search",
    "scroll",
    "near_vector",
    "query",
    "scan",
    "get",
}
WRITE_OPERATIONS = {
    "insert_one",
    "insert_many",
    "update_one",
    "update_many",
    "replace_one",
    "put",
    "upsert",
    "set",
    "index",
}
DESTRUCTIVE_OPERATIONS = {"delete_one", "delete_many", "drop", "truncate", "delete"}


def json_safe(value: Any) -> Any:
    """Return a deterministic JSON-safe copy of a native plan fragment."""

    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(json_safe(item) for item in value)
    return json.loads(json.dumps(value, default=str))


def document_safety(query: DocumentQuery, *, write_ops: Iterable[str] | None = None) -> Safety:
    """Infer safety from the document operation, never from artifact text."""

    writes = set(write_ops or WRITE_OPERATIONS)
    if query.operation in DESTRUCTIVE_OPERATIONS:
        return Safety(
            classification="destructive_write",
            unfiltered=not query.filter,
            destructive=True,
        )
    if query.operation in writes:
        return Safety(classification="write", unfiltered=not query.filter)
    return Safety(classification="read")


def unsupported_document_plan(
    adapter_id: str,
    engine_kind: str,
    query: DocumentQuery,
    diagnostics: list[AdapterDiagnostic],
) -> PlanDocument:
    """Build a supported=False plan with no artifact and stable metadata."""

    return PlanDocument(
        target_adapter=adapter_id,
        operation=query.operation,
        artifact=__import__("oq.ir", fromlist=["Artifact"]).Artifact(
            kind=engine_kind, text="", dialect=adapter_id
        ),
        parameters=[
            {"name": parameter.name, "type": parameter.type.kind} for parameter in query.parameters
        ],
        safety=document_safety(query),
        engine_plan=None,
        diagnostics=diagnostics,
        supported=False,
    )


def check_document_operation(
    query: DocumentQuery,
    supported: set[str],
    *,
    engine: str,
) -> list[AdapterDiagnostic]:
    if query.operation in supported:
        return []
    return [
        diagnostic(
            AdapterErrorCode.UNSUPPORTED,
            f"{engine} does not support operation {query.operation!r} in this adapter.",
            operation=query.operation,
            feature=query.operation,
            hint="Supported operations: " + ", ".join(sorted(supported)),
        )
    ]


def check_collection(
    query: DocumentQuery,
    known_collections: set[str],
    *,
    engine: str,
) -> list[AdapterDiagnostic]:
    if not known_collections or query.collection in known_collections:
        return []
    return [
        diagnostic(
            AdapterErrorCode.CAPABILITY,
            f"Collection {query.collection!r} is not present in the {engine} schema.",
            operation=query.operation,
            feature="collection",
            path=query.collection,
        )
    ]


def check_unsafe_filter_operators(
    value: Any,
    *,
    forbidden: set[str],
    engine: str,
    path: str = "filter",
) -> list[AdapterDiagnostic]:
    """Reject server-side script operators before any plan is executed."""

    diagnostics: list[AdapterDiagnostic] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and key in forbidden:
                diagnostics.append(
                    diagnostic(
                        AdapterErrorCode.UNSUPPORTED,
                        f"{engine} does not support server-side script operator {key!r}.",
                        feature=key,
                        path=path,
                    )
                )
            else:
                diagnostics.extend(
                    check_unsafe_filter_operators(
                        item, forbidden=forbidden, engine=engine, path=f"{path}.{key}"
                    )
                )
    elif isinstance(value, list):
        for position, item in enumerate(value):
            diagnostics.extend(
                check_unsafe_filter_operators(
                    item, forbidden=forbidden, engine=engine, path=f"{path}[{position}]"
                )
            )
    return diagnostics


def normalize_document(value: Any) -> Any:
    """Strip backend-only wrappers without dropping nesting."""

    if isinstance(value, dict):
        return {str(key): normalize_document(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_document(item) for item in value]
    return value
