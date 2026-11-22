# Querly Universal IR Specification

**Format version:** 0.1 (draft)
**Status:** Phase 0 design. Models and serialization are implemented in Phase 2.
**Related:** `docs/language/SPECIFICATION.md`, `ARCHITECTURE.md`

## 1. Purpose

The universal intermediate representation is the contract between the language
front end, the compiler, converters, adapters, and external tools. It is:

* **engine-independent** — no SQL text, no engine type names, no driver objects;
* **versioned** — every document declares its format version;
* **validated** — invalid documents are rejected as a whole;
* **stable** — field names and enum values are public API;
* **complete enough to be honest** — constructs that cannot be represented
  faithfully are not silently dropped; they are represented with an explicit
  unsupported marker in conversion reports.

The IR is not the AST. The AST preserves source syntax and spans; the IR is
canonical, resolved, and does not depend on which surface form produced it.

## 2. Envelope

Every serialized document shares an envelope:

```json
{
  "format": "querly.ir.query",
  "format_version": "0.1",
  "language_version": "0.1",
  "generator": {"name": "oq-compiler", "version": "0.1.0.dev0"},
  "document": { }
}
```

| Field | Type | Rules |
| --- | --- | --- |
| `format` | string | One of the document kinds below. |
| `format_version` | string | `MAJOR.MINOR`. Readers reject unknown majors and newer minors they do not understand. |
| `language_version` | string | Language version that produced the document. |
| `generator` | object | `name` and `version` of the producing tool. Never credentials, paths, or host names. |
| `document` | object | Kind-specific payload. |

Document kinds for format 0.1:

| `format` | Payload |
| --- | --- |
| `querly.ir.schema` | entities, attributes, constraints, indexes, relationships |
| `querly.ir.query` | one query or mutation, parameters, projections |
| `querly.ir.transaction` | ordered statements and isolation requirements |
| `querly.ir.migration` | version, up/down actions, reversibility |
| `querly.ir.plan` | physical strategy, required capabilities, safety classification |
| `querly.ir.compatibility` | per-construct conversion findings |

Serialization is UTF-8 JSON with deterministic key order. A binary encoding is
not defined in version 0.1.

## 3. Common value objects

### 3.1 Names

Names are preserved exactly as declared. The IR never applies case folding.
Adapters are responsible for quoting and for reporting engines that fold names.
A name has an optional `namespace` used by adapters for engine-level schemas;
language version 0.1 compiles imported declarations into a single flat
namespace.

### 3.2 Types

Types are canonical and tagged:

```json
{"kind": "integer"}
{"kind": "decimal", "precision": 38, "scale": 9}
{"kind": "text", "max_length": 255}
{"kind": "list", "element": {"kind": "text"}}
{"kind": "reference", "entity": "users", "cardinality": "many_to_one"}
```

Canonical kinds: `integer`, `float`, `decimal`, `text`, `boolean`, `date`,
`time`, `timestamp`, `duration`, `uuid`, `email`, `url`, `json`, `binary`,
`geo`, `list`, `set`, `map`, `vector`, `reference`.

Rules:

* `decimal` requires `precision >= 1` and `0 <= scale <= precision`.
* `text` and `binary` may carry `max_length >= 1`.
* `vector` requires `dimension >= 1`.
* `map` requires `key` and `value` types; keys must be scalar.
* `reference` requires `entity`, `cardinality` (`many_to_one`, `one_to_one`),
  and an optional inverse field name.
* Nullability is a property of an attribute or parameter, not of the type.

Aliases (`int`, `string`, `bool`, `number`) never appear in IR.

### 3.3 Expressions

Expressions are a tagged union. Operator names are canonical and
engine-independent.

```json
{"kind": "column", "entity": "users", "name": "age"}
{"kind": "literal", "type": {"kind": "integer"}, "value": 18}
{"kind": "binary", "operator": "greater_than", "left": {}, "right": {}}
{"kind": "call", "function": "sum", "arguments": []}
{"kind": "parameter", "name": "min_age", "type": {"kind": "integer"}}
```

Expression kinds for 0.1: `column`, `literal`, `parameter`, `binary`, `unary`,
`call`, `cast`, `case`, `list`, `map`, `field_access`, `index_access`,
`aggregate`.

Canonical operators:

| Category | Operators |
| --- | --- |
| Comparison | `equal`, `not_equal`, `less_than`, `less_or_equal`, `greater_than`, `greater_or_equal`, `like`, `in`, `between`, `is_null`, `is_not_null`, `is_empty` |
| Boolean | `and`, `or`, `not` |
| Arithmetic | `add`, `subtract`, `multiply`, `divide`, `modulo`, `negate` |
| Membership | `in`, `not_in` |

Literals are typed. `null` literals carry `{"kind": "null"}`. Decimal literals
carry a string value to avoid binary rounding; timestamp literals carry an ISO
8601 string plus an explicit `has_timezone` boolean. Implementation notes must
not parse decimal values through binary floats.

### 3.4 Spans

IR nodes carry an optional `origin` with `file`, `start_line`, `start_column`,
`end_line`, `end_column`. Origin is diagnostic metadata: removing it must not
change the meaning of a document, and converters must not rely on it.

## 4. Schema document

```json
{
  "entities": [
    {
      "name": "users",
      "attributes": [
        {
          "name": "id",
          "type": {"kind": "integer"},
          "nullable": false,
          "constraints": [{"kind": "auto"}]
        },
        {
          "name": "email",
          "type": {"kind": "email"},
          "nullable": false,
          "constraints": [{"kind": "unique"}]
        }
      ],
      "primary_key": ["id"],
      "indexes": [
        {"name": "ix_users_email", "columns": ["email"], "unique": true}
      ],
      "relationships": [
        {
          "name": "author",
          "kind": "many_to_one",
          "source_entity": "posts",
          "target_entity": "users",
          "on_delete": null,
          "on_update": null
        }
      ]
    }
  ],
  "enums": [],
  "extensions": []
}
```

Constraints are explicit and ordered. `nullable` is always present. A primary
key constraint is stored once per entity in `primary_key` (list of attribute
names) rather than repeated per attribute; composite keys are therefore
representable even though language version 0.1 permits only single-column
primary keys.

`extensions` holds opt-in engine extensions. Core tools must ignore unknown
extensions and may not treat them as portable.

## 5. Query document

```json
{
  "name": "adults",
  "operation": "select",
  "parameters": [{"name": "min_age", "type": {"kind": "integer"}}],
  "source": {"entity": "users", "alias": null},
  "joins": [],
  "projection": [
    {"expression": {"kind": "column", "entity": "users", "name": "id"}, "alias": null}
  ],
  "filter": null,
  "group_by": [],
  "having": null,
  "order_by": [{"expression": {}, "direction": "ascending", "nulls": "last"}],
  "limit": 50,
  "offset": 0,
  "distinct": false,
  "returning": []
}
```

Requirements:

* `operation` is `select`, `insert`, `update`, or `delete`.
* For `insert`, `rows` contains literal or parameter expressions only.
  Subqueries are not representable in language version 0.1.
* For `update`, `assignments` is an ordered list of `{attribute, expression}`.
* For `delete`, no projection is present.
* `order_by` null placement is always explicit after normalization.
* `limit` and `offset` are non-negative integers or absent.
* `projection` is never empty for `select`; a wildcard is expanded during
  normalization against the resolved schema, so the IR never contains `*`.

Mutating queries carry `safety`:

```json
{"classification": "write", "unfiltered": false, "destructive": false}
```

`classification` is one of `read`, `write`, `destructive_write`, `ddl`.
`unfiltered` is true when an update or delete has no filter. `destructive` is
true when the operation can remove data irrecoverably under the declared plan
(for example a table drop or a column drop).

## 6. Transaction document

```json
{
  "name": "transfer",
  "statements": [],
  "required_capabilities": ["transactions"],
  "rollback_on_error": true
}
```

Transactions contain query documents of mutation kind only. If the target
adapter lacks transaction capability, capability analysis fails; the IR is not
rewritten into independent statements.

## 7. Migration document

```json
{
  "name": "add_email",
  "version": 2,
  "entity": "users",
  "description": "add email column",
  "reversible": true,
  "up": [{"action": "add_column", "entity": "users", "attribute": {}}],
  "down": [{"action": "drop_column", "entity": "users", "attribute": "email"}],
  "data_loss_risk": false,
  "requires_transactional_ddl": true
}
```

`data_loss_risk` is computed from the actions and the inspected schema, not
from user intent. A migration that drops a populated column or table has
`data_loss_risk: true`. A migration with no `down` has `reversible: false` and
must be reported as such.

## 8. Plan document

A plan is the boundary between compiler and adapter:

```json
{
  "target": {"adapter": "postgres", "adapter_version": "0.1.0.dev0"},
  "operation": "select",
  "artifact": {"kind": "sql", "text": "SELECT ...", "dialect": "postgres"},
  "parameters": [{"name": "min_age", "placeholder": "$1", "type": {"kind": "integer"}}],
  "required_capabilities": ["parameter_binding", "order_by_nulls"],
  "safety": {"classification": "read", "unfiltered": false, "destructive": false},
  "limits": {"max_rows": 10000, "timeout_ms": 30000},
  "result": {"columns": [{"name": "id", "type": {"kind": "integer"}}]}
}
```

The artifact is data. It is never parsed by the compiler and never treated as
portable. `parameters` maps IR parameter names to engine placeholders; values are
supplied separately at execution time. A plan is not evidence of execution.

## 9. Compatibility document

```json
{
  "source": {"adapter": "postgres", "version": "16.2"},
  "target": {"adapter": "mysql", "version": "8.4"},
  "profile": "compatible",
  "status": "approximate",
  "findings": [
    {
      "construct": "attribute type email",
      "location": {"entity": "users", "attribute": "email"},
      "status": "compatible",
      "source": {"kind": "text", "max_length": 255},
      "target": {"kind": "text", "max_length": 255},
      "reason": "database-level email validation is unavailable; runtime validation retained"
    }
  ],
  "loss_of_constraints": ["email domain validation"],
  "requires_decision": []
}
```

Status ordering, most severe last: `exact`, `compatible`, `approximate`,
`unsupported`, `requires_decision`. The overall document status is the most
severe finding status. `best-effort` still never emits `exact` for an
approximate mapping.

## 10. Versioning rules

* Unknown fields in a document are preserved by tools that round-trip it and
  ignored by tools that consume it.
* Removing a field, renaming a field, changing an enum value, or changing the
  meaning of a field requires a major format version.
* Adding an optional field requires a minor format version.
* A reader that does not understand a newer minor version must reject the
  document; it must not guess.
* The IR never carries credentials, connection strings, or hostnames.

## 11. Open items before format 0.1 is frozen (Phase 2)

The following must be decided and tested in Phase 2, not guessed now:

1. The exact representation of `set` and `map` literals.
2. Whether enum types are first-class in format 0.1 or deferred.
3. The canonical form for naive timestamps.
4. Whether plans may include multiple artifacts for engines needing
   multi-statement DDL.

These are tracked in `KNOWN_LIMITATIONS.md` and must be resolved by tests in
Phase 2 before any adapter consumes the IR.
