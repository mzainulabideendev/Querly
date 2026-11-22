# Querly Universal Type System

**Status:** Phase 0 design. Normative for language version `0.1`, but **nothing in
this document is implemented**. The distribution `oq-types` is an empty scaffold:
it exports no types, performs no validation, and maps nothing to any engine. The
normative surface syntax and semantics live in
[`docs/language/SPECIFICATION.md`](../language/SPECIFICATION.md#type-system);
this document is the type-system contract that `oq-types`, the compiler, the
converter, and adapters must satisfy when they are built in Phases 2-4.

## 1. Purpose and boundaries

The universal type system exists so that one program has one meaning regardless
of the database it targets. It answers four questions:

1. What types may a Querly program name? (Section 3.)
2. What value does a type permit, and what is invalid? (Sections 4-7.)
3. How may types convert to each other inside the language? (Sections 6-7.)
4. What must an adapter prove when it maps a universal type to a native type?
   (Section 9.)

It deliberately does **not**:

* expose engine type names (`jsonb`, `varchar`, `AUTO_INCREMENT`, `oid`),
* decide storage layout, index method, or collation,
* model physical representations such as fixed-width records,
* permit adapter-specific type extensions in portable IR.

Engine types appear only in capability documents and generated artifacts. If a
universal type has no faithful native representation, the adapter reports
`unsupported` for that type rather than substituting a lossy one.

## 2. Canonical form and type expressions

A type expression has a canonical textual form used by diagnostics, tests, and
golden IR. It is produced by normalization, not typed by users.

```text
canonical_type  = scalar_type | collection_type ;
scalar_type     = "integer" | "float" | "decimal" [ "(" integer "," integer ")" ]
                | "text" [ "(" integer ")" ] | "boolean" | "date" | "time"
                | "timestamp" | "duration" | "uuid" | "email" | "url"
                | "json" | "binary" [ "(" integer ")" ] | "geo"
                | "vector" "(" integer ")" | "reference" "(" entity_name ")" ;
collection_type = ( "list" | "set" ) "<" type_expr ">"
                | "map" "<" type_expr "," type_expr ">" ;
```

Rules:

* Canonical form is lowercase, ASCII, with exactly one space after a comma in
  `decimal(p, s)` and `map<K, V>`, and no spaces inside angle brackets otherwise.
* Unparameterized `decimal` normalizes to `decimal(38, 9)`.
* Unparameterized `text` and `binary` mean **unbounded** and are distinct from
  any `text(n)` / `binary(n)`.
* Optionality is not part of the type. It is an attribute property, written
  explicitly in IR (`"nullable": true`) and never inferred.
* `reference(E)` is a relationship, not a primitive. Its canonical form embeds
  the target entity's resolved name, not an alias or a source spelling.
* Nesting depth is bounded by the implementation (provisional limit: 16). A
  deeper type is a `OQ-TYP-010` error, not a stack overflow.

## 3. Type catalogue

`order` states whether `<`, `<=`, `>`, `>=` are defined by the language. `eq`
states whether equality is defined. `null` means SQL-style three-valued logic
unless stated otherwise.

| Canonical type | Parameters | Order | Eq | Notes |
| --- | --- | --- | --- | --- |
| `integer` | none | yes | yes | Exact signed integer, at least 64-bit; adapter reports the actual range. |
| `float` | none | yes | yes | IEEE 754 binary64. `NaN != NaN`. `-0.0 = 0.0`. |
| `decimal(p, s)` | `1 <= p`, `0 <= s <= p` | yes | yes | Exact base-10. Scale is fixed by the type, not the value. |
| `text[(n)]` | `n >= 1` | yes (code-point) | yes | Unicode scalar values; length counts code points, never bytes. |
| `boolean` | none | yes (`false < true`) | yes | Two-valued; no unknown boolean. |
| `date` | none | yes | yes | Proleptic Gregorian calendar date, no zone. |
| `time` | none | yes | yes | Time of day, no date or zone. |
| `timestamp` | none | yes | yes | Instant when written with `Z`/offset; otherwise naive local time. |
| `duration` | none | yes | yes | Signed count of a defined unit. |
| `uuid` | none | no | yes | RFC 4122 canonical text, case-insensitive comparison. |
| `email` | none | yes (text) | yes | Text with an address shape constraint. |
| `url` | none | yes (text) | yes | Text with an absolute-URL shape constraint. |
| `json` | none | no | structural | Key order and number spelling are not significant for equality. |
| `binary[(n)]` | `n >= 1` | yes (bytewise) | yes | Raw bytes. |
| `geo` | none | no | yes | WGS 84 point. |
| `list<T>` | element type | no | element-wise, order-sensitive | Ordered, duplicates allowed. |
| `set<T>` | element type | no | element-wise, order-insensitive | Distinct values; `T` must be hashable. |
| `map<K, V>` | key and value types | no | key-wise | Keys must be hashable. |
| `vector(n)` | `n >= 1` | no | yes | Fixed-dimension numeric embedding; components are `float`. |
| `reference(E)` | entity name | no | identity | Many-to-one; the value is `E`'s primary key. |

Hashable types are the scalars except `json`, `geo`, `list`, and `map`.
`set` of a non-hashable type, or a `map` with a non-hashable key, is
`OQ-TYP-011`. `null` is never hashable and never a valid `set` element.

`json` equality compares parsed structure: object key order is irrelevant,
`1` and `1.0` are distinct, and duplicate keys are an error at literal level.
`geo` equality compares coordinates exactly as stored; distance ordering is a
function, not an operator.

## 4. Nullability

Nullability is an attribute property with three valid states in IR:
`"nullable": true`, `"nullable": false`, and (only for attributes with an
explicit default) `"nullable": false` plus a non-null default.

* Both surface forms default to **required**; neither form defaults to nullable.
* `@primary` implies required and unique.
* A nullable `@unique` attribute permits many rows whose value is `null`;
  uniqueness applies to non-null values.
* `null` propagates through arithmetic, comparison, and most functions following
  SQL three-valued logic. The exceptions are explicitly null-aware operators
  (`is null`, `is not null`, `coalesce`) and aggregates defined in the
  specification.
* `null` is never equal to `null` under `=`. Two nulls are "not distinct" only
  for uniqueness and grouping purposes.
* `null` is not a literal type. There is no `null` type, and a `null` literal
  requires a typed context to be valid (`OQ-TYP-012` otherwise).

## 5. Aliases and normalization

| Alias | Canonical | Scope |
| --- | --- | --- |
| `int` | `integer` | both forms |
| `string` | `text` | both forms |
| `bool` | `boolean` | both forms |
| `number` | `integer` | English-like only |
| `varchar(n)`, `char(n)` | `text(n)` | rejected in Querly source; accepted only by an import/conversion path |

Aliases are normalized before IR generation. The IR never contains an alias.
Two programs that differ only by alias must produce byte-identical IR, and a
test in Phase 3 must prove it.

`varchar`/`char` are listed only so that a future source-SQL importer has a
defined mapping. They are **not** valid Querly type names and the parser rejects
them with `OQ-TYP-013`.

## 6. Literals

| Literal | Default type | Context rule |
| --- | --- | --- |
| `42`, `-7` | `integer` | Decimal or float context widens implicitly. |
| `3.14` | `decimal` with the exact written scale | Float context widens to `float`. |
| `1.5e9` | `float` | Never narrows implicitly. |
| `"text"` | `text` | A literal may initialize `email`, `url`, `uuid`, `date`, `time`, `timestamp`, or `duration` if it parses; otherwise `OQ-TYP-014`. |
| `true` / `false` | `boolean` | none |
| `[a, b]` | `list<T>` | Element types unify; mixed types need an explicit `cast`. |
| `{a, b}` | `set<T>` | Same unification rule; duplicates are an error, not deduplication. |
| `{k: v}` | `map<K, V>` | Keys must be literals of a hashable type. |

Date, time, timestamp, and duration literals use ISO 8601 forms defined in the
specification's lexical section. A timestamp with `Z` or an offset is an
instant; a timestamp without one is naive and compares only with naive values.
Comparing an instant with a naive timestamp is `OQ-TYP-015`, never an implicit
assumption about the server's zone.

Integer literals outside the adapter's reported range are a compile-time
`OQ-TYP-016` warning and a runtime error, never silent truncation.

## 7. Conversions

### 7.1 Implicit (widening only)

* `integer` → `decimal` when the target scale can represent the value.
* `integer` or `decimal` → `float` when the other operand is `float`.
* Literal `text` → `email`, `url`, `uuid`, `date`, `time`, `timestamp`,
  `duration` when the context expects that type and the literal is valid.
* `decimal(p, s)` → `decimal(p2, s2)` when `p2 >= p` and `s2 >= s`.
* `text(n)` → `text` (unbounded) and `text(n)` → `text(m)` when `m >= n`.
* `binary(n)` → `binary` and `binary(n)` → `binary(m)` when `m >= n`.
* `T` → `T?` (a non-null value satisfies a nullable context).
* `list<T>` → `list<U>` only through element widening, which in version 0.1 is
  limited to the numeric and length rules above.

Everything else is an error. There is **no** implicit conversion between text
and numbers, booleans and numbers, text and binary, `json` and anything else, or
`reference` and its key type.

### 7.2 Explicit casts

`cast(expr as T)` is the only explicit conversion operator. It is defined for:

| From → To | Defined | Data-loss warning |
| --- | --- | --- |
| `integer` → `decimal`, `float` | yes | `float` may lose precision |
| `decimal` → `integer` | yes | truncation toward zero |
| `decimal` → `float` | yes | precision |
| `float` → `integer` | yes | truncation and range |
| `float` → `decimal` | yes | binary-to-decimal approximation; warning always |
| `text` → `integer`, `float`, `decimal`, `boolean`, `date`, `time`, `timestamp`, `duration`, `uuid`, `json` | yes, if parseable | never for a valid value; runtime error otherwise |
| `email`, `url`, `uuid`, `date`, `time`, `timestamp`, `duration` → `text` | yes | none (canonical form) |
| `json` → `text` | yes | canonical serialization; key order not preserved |
| `text` → `binary` and `binary` → `text` | **no** | use an explicit encoding function |
| `text` → `geo`, `vector` | **no** | use a constructor |
| anything → `reference` | **no** | references are declared, not cast |

A cast between unrelated types is `OQ-TYP-017`. A cast whose result cannot be
represented raises `OQ-TYP-018` at runtime. Casts never silently drop data
without a compile-time warning.

## 8. Canonical JSON encoding

IR and result metadata encode types as JSON objects, never as free strings:

```json
{"type": "decimal", "precision": 18, "scale": 4}
{"type": "text", "length": 200}
{"type": "text", "length": null}
{"type": "list", "element": {"type": "integer"}}
{"type": "map", "key": {"type": "uuid"}, "value": {"type": "json"}}
{"type": "vector", "dimensions": 768}
{"type": "reference", "entity": "users"}
```

Rules:

* Keys are emitted in the fixed order shown in the format specification.
* Absent parameters are `null`, never omitted, in serialized IR. (Absent
  *fields* are invalid; this is stricter than "unknown fields are preserved" for
  the envelope, where extension fields are allowed.)
* No engine names, no aliases, no nullable flags inside type objects.
* Unknown `"type"` values are a validation error, not a passthrough.

## 9. Adapter mapping obligations

Every adapter must, for every universal type, publish a capability entry with:

* `native` — the native type used, or `null` if unsupported;
* `support` — one of the conversion statuses below;
* `precision_notes` — the effective range, scale, length, or dimension;
* `collation_sensitive` — whether equality/order depends on collation;
* `tested` — whether the claim is exercised by the adapter's integration suite.

Mapping a universal type is a conversion finding, so the status vocabulary is
shared with [`docs/conversion/CONVERSION_GUIDE.md`](../conversion/CONVERSION_GUIDE.md)
and must never be softened:

| Status | Meaning for a type mapping |
| --- | --- |
| `exact` | The native type represents every value of the universal type, and the adapter proves it with tests, including boundaries. |
| `compatible` | The native type accepts every value with the same semantics, but with restrictions the report names (for example a required length bound). |
| `approximate` | Values can be stored but semantics differ (precision, ordering, or equality). Must say how. |
| `unsupported` | No faithful native representation. Compilation fails; no silent emulation. |
| `requires_decision` | More than one plausible mapping exists with different consequences; the user must choose. |

Examples of intended outcomes (not yet verified, and therefore not claims):

* `decimal(38, 9)` on a target whose native decimal maximum is 38 digits →
  `exact` **only** after range tests; otherwise `approximate`.
* `decimal` on a target offering only binary floating point → `approximate`,
  because equality and ordering change.
* `text` unbounded on a target with a hard row or column limit →
  `requires_decision`.
* `geo` on a relational engine without a spatial extension → `unsupported`.
* `vector(768)` on an engine without vector support → `unsupported`, never
  `text` with an application-side distance function.

`exact` is never inferred. It requires a proof obligation in the adapter's test
manifest, and the compatibility matrix records only tested statuses.

## 10. Validation and diagnostics

Validation is a pure function from a parsed type expression plus the enclosing
declaration context to either a canonical type or diagnostics. It must not touch
the network, a database, or the filesystem.

| Code | Condition |
| --- | --- |
| `OQ-TYP-001` | Unknown type name. |
| `OQ-TYP-002` | Parameter missing, extra, or not an integer literal. |
| `OQ-TYP-003` | `decimal` precision out of range or `scale > precision`. |
| `OQ-TYP-004` | `text(n)`/`binary(n)` with `n < 1`. |
| `OQ-TYP-005` | `vector(n)` with `n < 1` or above the adapter-reported limit. |
| `OQ-TYP-006` | `reference(E)` whose target is not a declared entity. |
| `OQ-TYP-007` | Duplicate or self-referential primary key declaration. |
| `OQ-TYP-010` | Type nesting deeper than the implementation limit. |
| `OQ-TYP-011` | `set`/`map` with a non-hashable element or key type. |
| `OQ-TYP-012` | Untyped `null` literal. |
| `OQ-TYP-013` | Engine type name used in Querly source. |
| `OQ-TYP-014` | Literal does not satisfy the constrained type in context. |
| `OQ-TYP-015` | Instant compared with naive timestamp. |
| `OQ-TYP-016` | Integer literal outside the adapter range. |
| `OQ-TYP-017` | Undefined cast. |
| `OQ-TYP-018` | Runtime cast result not representable. |

Codes follow the `OQ-AREA-NNN` rule from
[`docs/language/SPECIFICATION.md`](../language/SPECIFICATION.md#diagnostics).
Areas are stable; numbers are never reused. A diagnostic that changes meaning
gets a new number.

## 11. Determinism requirements (Phase 2 obligations)

The future `oq.types` package must guarantee, with tests:

1. Normalization is idempotent: `normalize(normalize(t)) == normalize(t)`.
2. Canonical strings and JSON are byte-stable for equivalent inputs, so IR
   golden tests can compare bytes.
3. Validation is total: every input string either yields a canonical type or
   diagnostics; it never raises an uncaught exception.
4. No adapter knowledge leaks in. The module must import no driver and no
   adapter package; tests enforce this statically.
5. Diagnostic ordering is deterministic for a given input.

## 12. Open questions for Phase 2

* Whether `set` and `map` are eligible for `@unique` and indexing on every
  engine, or only where the adapter proves it.
* How to represent `duration` in IR when a target stores only seconds: allow a
  canonical seconds form, or reject the type as `approximate`.
* Whether `email`/`url` are constraints over `text` (stored as text, checked at
  the boundary) or distinct storage types. Current design treats them as `text`
  with validation, and the capability report must say so.
* Enum types are not in language 0.1. If added, they need an IR representation
  that does not depend on engine enum ordering.
* Whether `geo` should be split into point/geometry/polygon in a later language
  version. Version 0.1 names only a point.

None of these open questions may be resolved by silently choosing an
approximation; each requires an ADR.
