# Querly Conversion Guide

**Status:** Phase 0 design. The converter is implemented in Phase 6. No
conversion works today.

## 1. Two workflows

### Schema conversion

```text
source database
  -> adapter inspection
  -> universal schema IR (with inspection findings)
  -> compatibility analysis against target capabilities
  -> target schema IR
  -> target DDL + migration plan
  -> compatibility report
```

Data migration is a separate, explicit step. Converting a schema never copies
data implicitly.

### Query conversion

```text
Querly source (or, later, source-engine SQL)
  -> parse and semantic analysis
  -> universal query IR
  -> capability analysis
  -> target artifact + binding plan
  -> validation (parse/plan check by the target adapter)
  -> compatibility report
```

A generated target artifact is validated by the target adapter before it is
presented as usable. Validation is not execution: the report says "validated",
never "executed".

## 2. Status vocabulary

Every finding has exactly one status:

| Status | Meaning | Example |
| --- | --- | --- |
| `exact` | Target representation is provably equivalent for every value the source permits. | `integer` (64-bit) to a 64-bit integer. |
| `compatible` | Values and observable results are preserved, with documented non-semantic differences. | `json` to a binary JSON type with different key ordering. |
| `approximate` | Some values, constraints, or guarantees are lost or weakened, and the loss is reported. | `text` to `varchar(255)` truncates longer values. |
| `unsupported` | No representation exists in the target. | `vector(1536)` without a vector extension. |
| `requires_decision` | A safe conversion depends on application intent. | Dropping a secondary unique constraint to satisfy an engine limit. |

Rules:

* `exact` requires a proof obligation, not a name match. Two types with the same
  name but different ranges are `approximate`.
* Loss of a constraint is never `compatible`. It is `approximate` at best, and
  must appear in `loss_of_constraints`.
* An `unsupported` finding aborts every profile, including `best-effort`.
* A `requires_decision` finding aborts `strict` and `compatible`, and is listed
  for the user in `best-effort` with the chosen default stated explicitly.

The overall report status is the most severe finding status, ordered
`exact` < `compatible` < `approximate` < `unsupported` < `requires_decision`.

## 3. Profiles

Profiles control refusal, never reporting:

| Profile | Allows | Aborts on |
| --- | --- | --- |
| `strict` | `exact` | any other status |
| `compatible` (default) | `exact`, `compatible` | `approximate`, `unsupported`, `requires_decision` |
| `best-effort` | `exact`, `compatible`, `approximate`, resolved `requires_decision` | `unsupported`, unresolved decisions |

`best-effort` must print every approximation before generating output and must
write the same findings into the report. It must not set `--force` semantics
silently.

## 4. Compatibility report

The report is versioned IR (`querly.ir.compatibility`) and is also rendered by
the CLI. A finding includes:

* the construct (`entity`, `attribute`, `constraint`, `index`, `query clause`);
* the location in the source schema or query;
* the source representation;
* the target representation or absence;
* the status and a human-readable reason;
* whether the difference affects values, constraints, performance, or only
  presentation.

Example CLI rendering:

```text
Conversion: PostgreSQL 16.2 -> MySQL 8.4 (profile: compatible)

APPROXIMATE  users.email              email/text(255) -> varchar(255)
             reason: MySQL has no email domain type; 255-character limit applied.
             impact: values longer than 255 characters are rejected.
             decision: use TEXT to preserve length, or accept the limit.

COMPATIBLE   users.id                 bigint -> bigint
EXACT        posts.id                 bigint -> bigint
UNSUPPORTED  posts.embedding          vector(1536) -> no vector type
             reason: target has no vector capability and no extension declared.

Result: blocked (1 unsupported). Nothing was executed.
```

## 5. Schema-level concerns

* **Names.** Engines differ in case folding and reserved words. The converter
  quotes where needed and reports renames that change the effective name.
* **Identity generation.** Universal `@auto` maps to the target's mechanism.
  The report states the mechanism and any behavioural difference (sequence gaps,
  transactional identity, etc.).
* **Defaults.** Expression defaults are converted only when the target supports
  the expression. Otherwise the finding is `approximate` (runtime default) or
  `unsupported` (no equivalent).
* **Checks.** Converted when the expression is portable; otherwise reported as
  runtime-only or unsupported. A dropped check is a constraint loss.
* **Foreign keys.** Referential actions that the source has and the language
  cannot express must be reported, not invented or dropped silently.
* **Indexes.** Prefix lengths, index method, partial predicates, and expression
  indexes are reported when they cannot be preserved.
* **Composite limits.** Engines limit index columns, key length, and identifier
  length. Exceeding a limit is a `requires_decision` or `unsupported` finding,
  never a silent truncation.

## 6. Query-level concerns

* **Null ordering.** If the target cannot express `nulls last/first`, the
  converter emits an explicit sort key (`IS NULL` projection) when possible and
  labels it `compatible`. If not possible, `approximate`.
* **Limits and offsets.** Engines differ in whether `offset` requires `limit`.
  A synthesized limit is reported.
* **Case sensitivity.** String comparison and `like` semantics differ by
  collation. The report names the collation assumption.
* **Division and rounding.** Integer division, decimal scale, and rounding mode
  differences are reported as `approximate` when results can differ.
* **`returning`.** Unsupported targets fail capability analysis.
* **Parameter binding.** Every target must bind parameters. A target that cannot
  is reported as unsupported rather than inlined.

## 7. Testing obligations (Phase 6)

* Property tests: converting a schema to itself is `exact` and produces
  equivalent IR.
* Round-trip tests where the language supports it; round trips that are not
  lossless must fail the "exact" assertion and document the reason.
* Golden report tests for every status.
* Integration tests that apply converted DDL to a real target and inspect it
  back, comparing universal IR rather than SQL text.
* Adversarial conversions: name collisions, reserved words, maximum identifier
  length, decimal precision overflow, and unsupported types.
* No test may assert success for a conversion that has an `unsupported` finding.
