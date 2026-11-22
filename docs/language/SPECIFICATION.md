# Querly Language Specification

**Language:** Querly
**Specification version:** 0.1 (draft)
**Status:** Phase 0 deliverable. Normative design. The lexer and parser that
enforce this specification are implemented in Phase 1; no component of the
language is executable yet.
**Grammar:** [`grammar.ebnf`](grammar.ebnf) is normative for syntax. This
document is normative for meaning. Where the two disagree, the disagreement is
a defect and must be fixed before Phase 1 is accepted.

## Scope

Querly is a database-independent language for describing schemas, querying and
mutating data, grouping changes into transactions, describing migrations, and
requesting conversion between database engines. A Querly program is compiled
into a database-independent intermediate representation (IR); adapters lower
that IR to a concrete engine. Querly never embeds engine-specific SQL in its
source language or in its IR.

The language has two surface forms with one meaning:

* **English-like** declarations, optimized for readability.
* **Structured** declarations, optimized for precision and tooling.

Both are parsed by the same lexer and parser, produce the same abstract syntax
tree shape, and normalize to the same IR. Mixing the two forms inside a single
declaration is an error. Mixing declarations in one compilation unit is
allowed.

This specification covers language version 0.1 only. Anything not stated here
is unspecified and must not be relied upon.

## Design principles

1. **One meaning, two spellings.** English-like and structured forms are
   surface syntax. They must not produce different semantics for equivalent
   programs.
2. **Engine independence.** Schema and query semantics are defined
   independently of any database. Engine behaviour is described by adapter
   capabilities, never by the core language.
3. **Explicit over implicit.** Optionality, uniqueness, reference targets, and
   conversion profiles are written down. The language does not guess whether a
   value is nullable or whether a conversion is lossy.
4. **No hidden data loss.** A construct that a target engine cannot represent
   exactly is reported as approximate or unsupported. It is never silently
   dropped or silently rewritten.
5. **Deterministic compilation.** Parsing and compilation never use a
   probabilistic model. An optional AI assistant may suggest programs, but its
   output is ordinary source text that must pass the same compiler.
6. **Safe by construction.** Values are always bound as parameters. Identifiers
   are resolved against a compiled schema, not interpolated from input. The
   language has no string-concatenated SQL.
7. **Readable diagnostics.** Every syntax or semantic problem carries a stable
   code, a source span, and a human-readable message.
8. **Honest boundaries.** Unsupported language features and unsupported engine
   features fail loudly and appear in the compatibility report.

## Compilation model

A Querly implementation performs these stages, in this order:

| Stage | Input | Output | Owner |
| --- | --- | --- | --- |
| Source reading | bytes | validated UTF-8 text, offset map | `oq-core` |
| Lexing | text | tokens with spans and trivia | `oq-core` |
| Parsing | tokens | cursorless AST | `oq-core` |
| Semantic analysis | AST | resolved semantic model | `oq-core` |
| Normalization | semantic model | canonical form | `oq-compiler` |
| IR generation | canonical model | versioned universal IR | `oq-ir` |
| Capability analysis | IR + adapter capabilities | findings | `oq-compiler` |
| Planning | IR + findings | physical plan | `oq-compiler` |
| Emission | physical plan | engine artifacts | adapter |
| Execution | artifacts + parameters | normalized result | `oq-runtime` |

The AST, semantic model, IR, and physical plan are distinct data structures.
None may be substituted for another, and no stage may read engine-specific
syntax out of the IR.

## Lexical structure

### Encoding

Source files are UTF-8. A leading byte-order mark is accepted and discarded. A
NUL character is rejected as binary or mis-encoded content. Other encodings are
not part of language version 0.1.

### Whitespace, line breaks, and comments

* Space and tab are trivia.
* Line breaks are `\n`, `\r\n`, or `\r`. A run of line breaks is lexed as a
  single `newline` token.
* `--` starts a line comment that ends at the next line break.
* `/*` starts a block comment that ends at the next `*/`. Block comments do not
  nest.
* Comments and whitespace are discarded except for their effect on `newline`
  tokens and source spans.

`newline` tokens are significant only inside English-like schema fields,
English-like mutations, and migration bodies. Structured declarations treat
them as trivia.

### Keywords and identifiers

Keywords are case-insensitive. Identifiers are case-sensitive and preserve
their declared spelling. An identifier begins with a Unicode `XID_Start`
character or `_`, and continues with `XID_Continue` characters.

Structural keywords (for example `table`, `query`, `from`, `where`, `order`,
`migration`) are reserved. A name that collides with a structural keyword must
be written as a quoted identifier in backticks:

```sql
table `order` {
    id: integer @primary
}
```

Type names (`integer`, `email`, `vector`, ...) and field modifiers
(`required`, `unique`, ...) are contextual keywords: they are recognized only
where the grammar expects a type or modifier. A column may therefore be named
`email` and typed `email`:

```sql
table users {
    email: email @required
}
```

String literals use double quotes. Backticks quote identifiers. Single quotes
carry no special meaning in language version 0.1.

### Literals

| Kind | Example | Notes |
| --- | --- | --- |
| integer | `42`, `-7` | Unary minus is an operator, not part of the literal. |
| decimal | `12.50` | Exact decimal, not binary floating point. |
| float | `1.5e3` | Binary64 when used in float context. |
| string | `"Ada"` | Escapes: `\" \\ \/ \b \f \n \r \t \uXXXX`. No multi-line strings. |
| boolean | `true`, `false` | |
| null | `null` | The absent value. |
| date | `2024-02-29` | Exact shape `YYYY-MM-DD` is a date, never subtraction. |
| time | `23:59:59` | Local time of day, no zone. |
| timestamp | `2024-02-29T23:59:59Z` | Time zone is optional; absence means naive local time. |
| duration | `500ms`, `15min`, `2h`, `3d`, `1w` | Unit is required. |
| list | `[1, 2, 3]` | Element type is inferred from context. |

The lexer uses maximal munch. `2024-01-01` lexes as one date token, while
`2024 - 1` lexes as integer, minus, integer.

### Source positions

Offsets are zero-based code-point indices. Lines and columns are one-based.
Columns count code points, not display cells. A span is half-open: the start is
included and the end is excluded. Every token, AST node, IR node produced from
source, and diagnostic carries a span where a source position exists.

## Declarations

A compilation unit is a sequence of top-level declarations:

```sql
import "billing.oq" as billing

table users { ... }
query adults { ... }
transaction transfer { ... }
migration add_email { ... }
convert schema from postgres to mysql { ... }
```

### Imports

`import "relative/path.oq" [as alias]` makes the declarations of another file
available. Paths are resolved relative to the importing file. Cyclic imports
are an error. Importing the same file twice with different aliases is allowed;
its declarations are the same entities.

Imports are compiled before the importing unit. An import that fails to compile
prevents the importing unit from compiling.

### Schemas: structured form

```sql
table users {
    id: integer @primary @auto
    name: string @required
    email: email @required @unique
    age: integer?
}
```

An attribute is `name: type`, an optional `?`, and zero or more modifiers.
`?` and `@required` are mutually exclusive. As stated under **Optionality**,
an attribute with neither marker is required, in both surface forms. A table
may also declare composite indexes and composite uniqueness constraints:

```sql
table people {
    first_name: text @required
    last_name: text @required
    email: email
    index ix_people_name (last_name, first_name)
    unique (email)
}
```

### Schemas: English-like form

```text
create table users
with:
    id as unique number
    name as required text
    email as required email
    age as optional number
```

Each field is exactly one physical line: `name as [modifiers] type
[modifiers]`. Fields may be preceded by descriptors in any order (`required`,
`optional`, `unique`, `auto`, `primary key`, `default <expr>`, `check <expr>`,
`references <entity>`). Line breaks are part of the syntax, so a field
expression must fit on its line. Composite constraints use their own lines:

```text
create table people
with:
    first_name as required text
    last_name as required text
    email as optional email
    make index ix_people_name on (last_name, first_name)
    keep (email) unique
```

### Equivalence of the two forms

The structured example above and this English-like program must produce
identical IR, ignoring source spans:

```text
create table users
with:
    id as primary key auto integer
    name as required text
    email as required unique email
    age as optional integer
```

Mapping rules used by normalization:

| English-like | Structured | Meaning |
| --- | --- | --- |
| `required` | `@required` | not null, must be supplied unless a default exists |
| `optional` | `?` | nullable |
| `unique` | `@unique` | at most one non-null value per table |
| `auto` | `@auto` | engine-generated value on insert |
| `primary key` | `@primary` | primary key; implies `required` and `unique` |
| `default <expr>` | `@default(<expr>)` | default when the value is omitted |
| `check <expr>` | `@check(<expr>)` | row-level validation |
| `references <entity>` | `@references(<entity>)` | foreign-key target |
| `number` | `integer` | canonical 64-bit-or-wider integer |

The English-like form deliberately has no spelling for `text(n)`,
`binary(n)`, `decimal(p, s)`, `vector(n)`, or `map<K, V>` field types beyond
the `decimal(p, s)` and `vector(n)` forms shown in the grammar. Programs that
need those constraints use the structured form. This is an expressiveness
difference, not a semantic difference: any declaration accepted in both forms
normalizes identically.

### Constraints and modifiers

* `@primary` — exactly one primary key per entity in language version 0.1.
  Implies required and unique. `@auto` is permitted on a single-column integer
  primary key; adapters choose identity, sequence, or equivalent and report
  which mechanism is used.
* `@required` — value must be present on insert unless `@default` is declared.
  Maps to a NOT NULL constraint where the engine has one. Document stores must
  be validated by the runtime, and the adapter must say so in its capability
  report.
* `@unique` — duplicate non-null values are rejected. Null values are not
  considered duplicates. Engines that behave differently must report the
  divergence.
* `@default(<expr>)` — expression is evaluated on insert when the attribute is
  omitted. Defaults must be deterministic; `now()` is permitted. References to
  other rows are not permitted in defaults.
* `@check(<expr>)` — evaluated on insert and update. The expression may only
  reference attributes of the same row plus parameters. The adapter must
  report whether the check is enforced natively or by the runtime.
* `@index` — non-unique single-column index. Composite indexes use
  `index <name> (<columns>)`.
* `@references(<entity>)` — makes the attribute a many-to-one relationship.
  Deterministic `on delete` / `on update` behaviour is not yet expressible in
  language version 0.1; migrations that need cascade rules must be written in
  engine-specific migration steps, which language version 0.1 does not provide.
  This gap is tracked in `KNOWN_LIMITATIONS.md`.

### Relationships

Relationships are declared structurally, using the same attribute syntax in
both forms:

* Many-to-one: `author: reference(users) @required` — each row references one
  `users` row.
* One-to-many: `posts: list<reference(posts)>` — the inverse side; it is a
  navigation declaration, materialized as a query, not a stored column.
* One-to-one: a many-to-one reference with `@unique`.
* Many-to-many: an explicit join entity whose primary key is the pair of
  references:

```sql
table post_tags {
    post: reference(posts) @primary
    tag: reference(tags) @primary
}
```

The English-like spelling is `reference to <entity>` and
`list of reference to <entity>`.

Referential actions, deferred constraints, and polymorphic relationships are
outside language version 0.1.

### Indexes

`index <name> (<column> [, ...]) [@unique]` declares an index. Index names are
unique per entity. An index over zero columns is an error. An index referencing
an unknown column is an error. Whether an index is used for a query is a
planning decision and must never be assumed by the language.

## Query language

### Structured selects

```sql
query adults {
    from users
    select id, name, age
    where age > 18 and name like "A%"
    order by name asc, age desc
    limit 50 offset 0
}
```

Clause order is fixed: `from`, joins, `select`, `where`, `group by`, `having`,
`order by`, `limit`, `offset`. `limit` and `offset` are non-negative integer
literals. `offset` without `limit` is an error. `nulls first` and `nulls last`
may follow any ordering term.

### English selects

```text
get all users where age is greater than 18 sort by name ascending
```

The entity follows `all` directly. A projected form names the columns:

```text
get id, name from users where age is at least 18 take 20
```

`get first 5 ...` limits the result to five rows. `take n` sets the limit;
`skip n` sets the offset. `sort by` terms accept `ascending` or `descending`.

### Operator equivalence

| Structured | English-like | Universal operator |
| --- | --- | --- |
| `=` | `is equal to`, or `is <value>` | equality |
| `!=`, `<>` | `is not equal to`, `is not <value>` | inequality |
| `>` | `is greater than` | greater than |
| `>=` | `is at least` | greater than or equal |
| `<` | `is less than` | less than |
| `<=` | `is at most` | less than or equal |
| `like` | `is like` | pattern match |
| `in (...)` | `is one of (...)` | set membership |
| `between a and b` | `is between a and b` | inclusive range |
| `is null` | `is null` | null test |
| `is not null` | `is not null` | negated null test |
| `isEmpty(x)` | `is empty` | empty text or empty collection |
| `not isEmpty(x)` | `is not empty` | non-empty text or collection |
| `not`, `and`, `or` | `not`, `and`, `or` | boolean connectives |

Word forms accept `not` after `is` (`is not greater than`). Symbolic operators
are also accepted in English-like clauses, and word forms are accepted in
structured clauses; both normalize to the same IR. Tooling may warn about
stylistic mixing.

### Three-valued logic

Querly uses SQL-style three-valued logic. Comparisons with `null` evaluate to
`unknown`, not `false`. `and` returns `false` if either side is `false`, `true`
if both are `true`, and `unknown` otherwise. `or` returns `true` if either side
is `true`, `false` if both are `false`, and `unknown` otherwise. `not unknown`
is `unknown`. A `where` clause keeps only rows for which the predicate is
`true`; `unknown` and `false` are discarded. `= null` is a compile error;
`is null` is the only null test. `isEmpty` tests whether text or a collection
has zero elements, and returns `unknown` when its operand is `null`; it does
not test for null.

### Aggregations

```sql
query order_totals {
    from orders
    select customer_id, count(*) as orders, sum(total) as revenue
    group by customer_id
    having sum(total) > 1000
    order by revenue desc
}
```

Universal aggregate functions are `count`, `count(*)`, `sum`, `avg`, `min`,
and `max`. `count(*)` counts rows; `count(expr)` counts non-null values. `sum`
and `avg` require a numeric argument. `avg` returns a decimal for integer and
decimal inputs and a float for float inputs. A projection that mixes aggregated
and non-aggregated expressions requires a `group by` covering every
non-aggregated expression. `having` may only reference grouped expressions or
aggregates. `distinct` aggregates, window functions, and grouped sets are not
part of language version 0.1.

### Sorting, pagination, and ordering

Ordering is deterministic only when the ordering terms form a total order. The
runtime does not guarantee an order for ties. Default null placement is `nulls
last` for ascending and `nulls first` for descending; both can be stated
explicitly. Negative `limit` or `offset` values are errors. A limit of zero
returns no rows.

### Mutations

Insert:

```sql
query add_user {
    insert into users (name, email) values ("Ada", "ada@example.com") returning id
}

query add_many {
    insert into users (name, email)
    values ("Ada", "ada@example.com"), ("Grace", "grace@example.com")
}
```

Update and delete:

```sql
update users set name = "Ada Lovelace", age = 36 where id = 1 returning id
delete from users where id = 1
```

English-like mutations are one physical line each:

```text
add to users: name to "Ada", email to "ada@example.com"
change users set name to "Ada Lovelace" where id is 1
remove from users where id is 1
```

An `update` or `delete` without a `where` clause affects every row. The
compiler emits a warning for this, and the runtime requires an explicit
`--allow-unfiltered` acknowledgement; in read-only mode it is rejected. Every
mutation reports affected row counts. `returning` is supported only when the
target adapter reports that capability; otherwise capability analysis fails
the compilation rather than approximating the result.

`insert` must list every required attribute that has no default. Unknown
columns, duplicate columns, arity mismatches, and values that are not assignable
to the attribute type are compile errors.

### Parameters

```sql
query find_user(id: integer) {
    from users select id, name where id = $id
}
```

Parameters are declared in the query header and referenced as `$name`.
Parameters are always bound by the driver; there is no interpolation path in
the language, the IR, or the runtime. A declared parameter that is unused, a
reference to an undeclared parameter, and a duplicate parameter name are
compile errors. Parameter types are universal types and are checked against
their use sites.

### Transactions

```sql
transaction transfer {
    update accounts set balance = balance - 100 where id = 1
    update accounts set balance = balance + 100 where id = 2
}
```

```text
begin transaction transfer
change accounts set balance to balance - 100 where id is 1
change accounts set balance to balance + 100 where id is 2
commit
```

Transactions contain mutations only; read-only statements are not part of a
transaction in language version 0.1. A transaction is atomic where the adapter
reports transaction support. If the adapter reports no transactions, compiling
a transaction declaration fails; it is never executed as a sequence of
independent statements. Isolation level, retry policy, and savepoints are
runtime configuration, not language syntax. Every statement in a transaction is
executed in order; the first failure aborts and rolls back, and the error names
the failing statement.

### Migrations

```sql
migration add_email {
    version 2
    entity users
    description "add email column"
    up {
        alter table users add column email: email @required @default("")
    }
    down {
        alter table users drop column email
    }
}
```

A migration has a monotonically increasing integer version, an optional
entity, an optional description, and one `up` block. `down` is optional, but a
migration without `down` is irreversible and the planner must say so. Supported
actions in version 0.1 are: create a table, add/drop/rename a column, change a
column type, set/drop not-null, set a default, drop a table, create and drop an
index. Each action must be verifiable against the inspected schema. A migration
that would drop a column or table containing data requires explicit data-loss
acknowledgement before execution. Migrations execute as one transaction when
the adapter supports transactional DDL, and the planner reports when it does
not.

### Database conversion

```sql
convert schema from postgres to mysql {
    profile compatible
    entities users, posts
}
```

A conversion request names two installed adapters and a profile. Profiles are
`strict`, `compatible`, and `best-effort`:

* `strict` fails on any construct that is not exact.
* `compatible` accepts documented, semantics-preserving mappings and fails on
  everything else.
* `best-effort` generates the closest available representation and reports
  every approximation; it still fails on constructs with no representation at
  all.

Conversion always produces a compatibility report. A status of `exact` is only
allowed when the target representation is provably equivalent for all values
permitted by the source construct. A declaration does not itself migrate data;
it produces a plan that must be reviewed and executed explicitly.

## Type system

### Universal types

| Universal type | Meaning |
| --- | --- |
| `integer` | Exact integer. Implementations support at least signed 64-bit; adapters report their actual range. |
| `float` | IEEE 754 binary64. |
| `decimal(p, s)` | Exact decimal. `p >= 1`, `s >= 0`, `s <= p`. Bare `decimal` is `decimal(38, 9)`. |
| `text` | Variable-length Unicode text. |
| `text(n)` | Text with a maximum length of `n >= 1` code points. |
| `boolean` | Two-valued boolean. |
| `date` | Calendar date without a time or zone. |
| `time` | Time of day without a date or zone. |
| `timestamp` | Date and time; with `Z`/offset it is an instant, without it is naive local time. |
| `duration` | Signed duration with a defined unit. |
| `uuid` | RFC 4122 UUID. |
| `email` | Text constrained to an email address shape. |
| `url` | Text constrained to a URL shape. |
| `json` | Structured JSON value; key order is not significant. |
| `binary` | Variable-length bytes. |
| `binary(n)` | Bytes with a maximum length of `n`. |
| `geo` | Geographic point in WGS 84. |
| `list<T>` | Ordered sequence of `T`. |
| `set<T>` | Unordered collection of distinct `T`. |
| `map<K, V>` | Mapping from `K` to `V`. |
| `vector(n)` | Fixed-length numeric embedding of dimension `n >= 1`. |
| `reference(E)` | Many-to-one relationship to entity `E`. |

### Aliases

| Alias | Canonical type |
| --- | --- |
| `int` | `integer` |
| `string` | `text` |
| `bool` | `boolean` |
| `number` (English-like only) | `integer` |

Aliases are normalized before IR generation. The IR only contains canonical
types.

### Optionality

`T?` (structured) and `optional T` (English-like) mean nullable. `@required`
(structured) and `required T` (English-like) mean non-null. A type with neither
spelling is **required in both forms**; the two forms differ only in spelling,
never in the default, so equivalent programs cannot disagree about nullability.
`@primary` implies required and unique. The normalizer records the resolved
optionality explicitly, so downstream stages never apply a default.

A nullable attribute that is also `@unique` permits multiple rows with `null`;
uniqueness applies to non-null values only.

### Numeric rules

* Integer arithmetic is exact within the adapter's reported range. Overflow is
  a runtime error, not a wrap-around, unless the adapter reports wrapping
  behaviour and the user selected a profile that accepts it.
* Decimal arithmetic is exact and follows the declared precision and scale.
  Rounding mode is `half-even` unless a runtime policy overrides it, and the
  override is recorded in the execution plan.
* Float arithmetic is IEEE 754 binary64. NaN compares unequal to everything,
  including itself. Float equality in a `where` clause is permitted but may be
  flagged as a warning by tooling.
* Division of two integers produces a decimal, not a truncated integer.
  Integer division is written `div(a, b)` when a target needs it.
* `sum` of an empty set is `null`; `count` of an empty set is `0`; `avg` of an
  empty set is `null`.

### Comparison and typing rules

Comparisons require operands of compatible types. The only implicit conversions
are:

* integer to decimal,
* integer or decimal to float when the other operand is float,
* text literal to `email`, `url`, `uuid`, `date`, `time`, `timestamp`, and
  `duration` when the context expects that type and the literal is valid.

No implicit conversion between text and numbers, booleans and numbers, or
binary and text is allowed. Explicit conversion uses `cast(expr as type)`.
Casting that can lose information (for example `text` to `integer`, or
`decimal` to `integer`) is permitted but is a compile-time warning; casting
that is not defined by the language is an error.

### Literal defaults and null

An attribute with neither `@required` nor a default may be omitted on insert
and becomes `null`. A required attribute without a default must be supplied.
A `null` value never satisfies `@check`, so checks on optional attributes must
guard with `is not null` when null should be accepted.

## Evaluation semantics

* Expressions evaluate left-to-right for `and`/`or` only where short-circuiting
  cannot change the result under three-valued logic. Implementations must not
  depend on side effects in expressions; language version 0.1 has no
  side-effecting functions.
* A query is evaluated against a consistent snapshot where the adapter supports
  snapshot isolation. If it does not, the adapter must report that reads may
  observe concurrent writes.
* Result rows are ordered only when an `order by` makes the order total.
* Result column names follow projection aliases when present, otherwise the
  referenced attribute name. Duplicate result names are an error unless the
  adapter reports that it can disambiguate them, in which case the runtime
  appends a deterministic suffix and records it in the result metadata.
* The runtime applies a maximum result-row limit (configuration, default
  `10_000`) and a statement timeout (default `30s`). Exceeding either is an
  error, never a silently truncated result.

## Diagnostics

Every diagnostic has a stable code of the form `OQ-AREA-NNN`, a severity
(`error`, `warning`, `info`, `hint`), a message, an optional source span, and
optional notes. Example:

```text
error[OQ-PARSE-004]: expected ')' after parameter list
  --> app.oq:12:18
   |
12 | query find(id: integer {
   |                      ^
   = note: query parameters are parenthesized
```

Compilation succeeds only when no error-severity diagnostic exists. Warnings do
not block compilation unless the caller selects a strict policy. An
implementation must not discard diagnostics that it cannot render; it must emit
them without a source snippet.

## Versioning

The language version, grammar version, and IR format version are distinct.
Language `0.1` pairs with IR format `0.1`. Additive language changes increase
the minor version and must be representable in the existing IR version, or
require an IR minor bump. Removing or changing the meaning of a construct
requires a major version. The IR documents its own format version and the
language version that produced it; readers must reject unknown major versions
and must preserve unknown optional fields when round-tripping a document.

## Conformance

A conforming implementation must:

1. Accept every program admitted by the grammar and reject programs the
   grammar excludes, with a positioned diagnostic.
2. Produce identical normalized IR for equivalent English-like and structured
   programs.
3. Reject a program before execution if semantic analysis or capability
   analysis reports an error, and perform no database side effects in that case.
4. Bind all values as parameters and resolve all identifiers against compiled
   declarations.
5. Refuse to execute destructive changes without explicit acknowledgement.
6. Report compatibility per construct, never as a blanket claim.
7. Preserve the distinction between generated artifacts and successfully
   executed operations in all output, logs, and APIs.

## Known gaps in language version 0.1

These are deliberate scope boundaries, not silent omissions:

* English-like syntax for migrations and conversion requests is deferred.
* Subqueries, common table expressions, unions, `distinct`, window functions,
  and `group by` sets are not in version 0.1.
* Referential actions (`on delete cascade`, ...) and deferred constraints are
  not expressible.
* Database namespaces (PostgreSQL schemas, MySQL databases as namespaces) are
  represented by imports, not by a qualified naming syntax.
* Partial and expression indexes are not expressible.
* `update ... from`, `insert ... select`, and upserts are not expressible.
* Isolation levels and retry policies are runtime configuration.
* Stored procedures, triggers, and user-defined functions are out of scope.

Every gap above is tracked in `KNOWN_LIMITATIONS.md` with the phase that will
address it or the reason it is out of scope.
