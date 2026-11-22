# Querly Architecture

**Status:** Phase 0 foundation. This document describes the target architecture
and marks which parts exist today.

## 1. What Querly is

Querly is a database-independent programming language and toolchain. Developers
write schemas, queries, mutations, transactions, migrations, and conversion
requests in Querly. The toolchain parses and checks them, lowers them to a
versioned universal IR, checks adapter capabilities, generates engine artifacts,
and executes them through a controlled runtime.

Generated artifacts are not execution. Every layer keeps "compiled" and
"executed" distinct in its APIs, logs, and status reporting.

## 2. Repository layout

```text
Querly/
  packages/                  Python distributions, all sharing the `oq` namespace
    oq-core/                 lexer, parser, AST, semantic analysis, diagnostics
    oq-ir/                   versioned universal IR models and serialization
    oq-types/                universal type system and type mappings
    oq-compiler/             normalization, capability analysis, planning, emission
    oq-runtime/              connections, execution, transactions, security policy
    oq-converter/            schema and query conversion, compatibility reports
    oq-migrations/           migration planning, verification, checkpoints
    oq-cli/                  the `oq` command
    oq-sdk-python/           Python embedding API
    oq-ai/                   optional natural-language assistant
  adapters/                  independent adapter distributions (Phase 4+)
  docs/                      specifications and guides
  tests/                     cross-package invariant tests
  examples/                  runnable examples (added with the features they show)
```

Every Python distribution installs into the same top-level `oq` namespace
(PEP 420). There is deliberately no `oq/__init__.py`, so installing one package
does not shadow another. Adapters use their own namespace, `oq_adapters`, and
are discovered through Python entry points.

## 3. Dependency direction

Dependencies point inward, toward the language core:

```text
oq-cli ─────────────┐
oq-sdk-python ──────┤
oq-ai ──────────────┤
oq-migrations ──────┤
oq-converter ───────┤
oq-runtime ─────────┼──> oq-compiler ──> oq-ir ──> oq-core
                    │         │            │
oq-types ───────────┘         └──> oq-types ┘

adapters/oq-adapter-* ──> adapter contract (oq-runtime) + oq-ir
```

Hard rules enforced by tests and review:

* `oq-core` depends on nothing but the standard library. It must not import a
  database driver, an HTTP client, a model provider, or a CLI framework.
* `oq-ir` may depend on `oq-core` and validation libraries, never on a driver.
* `oq-types` is dependency-free at runtime.
* `oq-compiler` may depend on the IR and types, never on a concrete adapter.
  It talks to adapters only through the abstract capability and emission
  interfaces.
* Adapters depend on the published contract packages. The core never imports an
  adapter. Adapter selection is driven by configuration and entry points.
* `oq-cli` is the only package that may format terminal output and exit codes.
  Library packages return structured results.

## 4. Compiler pipeline

| # | Stage | Responsibility | Failure mode |
| --- | --- | --- | --- |
| 1 | Source reading | decode UTF-8, build offset table, reject binary content | `SourceError` |
| 2 | Lexing | tokens, trivia, newline significance, literal decoding | error diagnostic |
| 3 | Parsing | build the AST, enforce grammar, recover locally | error diagnostics, partial AST |
| 4 | Semantic analysis | resolve names, check types, check constraints, resolve relationships | error diagnostics, no model |
| 5 | Normalization | canonicalize aliases, optionality, operator spelling, declaration order | cannot fail for valid models |
| 6 | IR generation | versioned schema and query documents | structural validation error |
| 7 | Capability analysis | compare IR requirements with adapter capabilities | findings with compatibility status |
| 8 | Planning | choose a physical strategy, parameter binding plan, safety checks | planning error |
| 9 | Emission | adapter generates engine artifacts | adapter error with normalized code |
| 10 | Execution | runtime binds parameters, enforces policy, executes, normalizes results | execution error, rollback |

Stages 1-4 live in `oq-core`. Stage 5-8 live in `oq-compiler`. Stage 9 belongs
to adapters. Stage 10 lives in `oq-runtime`.

### AST, semantic model, IR, plan

These are four different trees with different invariants:

* **AST** is close to source. It contains both syntax forms, unresolved names,
  and precise spans. It may be incomplete after a parse error.
* **Semantic model** is name-resolved and type-checked. It still follows the
  AST shape but every reference points at a declaration.
* **IR** is canonical, versioned, serializable, and engine-independent. It has
  no source syntax, no spans required for meaning, and no SQL. It uses canonical
  type names, canonical operator names, and explicit optionality.
* **Physical plan** is adapter-specific. It contains artifacts (SQL text,
  aggregation pipelines, commands), parameter placeholders, expected result
  metadata, and required capabilities. It is never serialized as portable IR.

No later stage may re-parse source. No earlier stage may construct a physical
plan.

## 5. Universal type system and IR

The universal type system is specified in
[`docs/types/TYPE_SYSTEM.md`](docs/types/TYPE_SYSTEM.md). It fixes the canonical
type set, canonical strings and JSON encoding, nullability as an attribute
property, implicit widening rules, explicit casts, and the proof obligations an
adapter owes when it maps a universal type to a native one. Engine type names
never appear in portable IR; they appear only in capability documents and
generated artifacts.

The IR is specified in [`docs/ir/IR_SPECIFICATION.md`](docs/ir/IR_SPECIFICATION.md).
Key properties:

* Versioned documents with `format`, `format_version`, and `language_version`.
* Separate document kinds: schema, query, transaction, migration, plan,
  compatibility report.
* Validated models: an IR document is either valid or rejected as a whole.
* Stable field names and explicit nullability. No implicit defaults in
  serialized form.
* No engine-specific names. A PostgreSQL `jsonb` or MySQL `AUTO_INCREMENT` never
  appears in the IR; the IR records the universal intent and the adapter picks
  the mechanism.

The IR is a public contract. Third-party tools may read and write it. Breaking
it requires a major version bump and a migration note.

## 6. Adapter architecture

Adapters implement a documented contract covering connection lifecycle, schema
inspection, schema compilation, query compilation, parameterized execution,
transactions, capability reporting, result normalization, error normalization,
and version detection. The contract is described in
[`docs/adapters/ADAPTER_GUIDE.md`](docs/adapters/ADAPTER_GUIDE.md).

An adapter must publish a capability document that is machine-readable, versioned,
and honest. Capability claims are tested in CI against real disposable database
instances (Docker Compose for relational and document stores, service
containers for others). An adapter without passing integration tests is marked
experimental and must not be advertised as supported.

Adapters live in `adapters/oq-adapter-<engine>` and are independently
installable. Entry point group: `oq.adapters`. The CLI and SDK discover adapters
by group and id, never by importing a hardcoded module.

## 7. Runtime and security boundary

The runtime is the only place that touches a live database. It owns:

* connection creation and credential handling (delegated to drivers, never
  logged),
* parameter binding and result normalization,
* transaction boundaries and rollback,
* read-only enforcement,
* destructive-operation acknowledgement,
* statement timeouts and cancellation,
* row limits,
* audit logging with secret and value redaction,
* error normalization.

Policy is checked before execution, not by inspecting generated SQL after the
fact. Every execution plan declares whether it is a read, a write, a
destructive write, or DDL. A read-only policy rejects anything but reads. A
policy requiring confirmation rejects destructive plans unless a confirmation
token is supplied.

## 8. Conversion architecture

Two independent workflows share the IR:

**Schema conversion:** inspect source schema → universal schema IR → capability
diff → target schema IR → target DDL + migration plan + compatibility report.

**Query conversion:** parse source Querly (or convert from source-engine SQL in
a later phase) → semantic model → universal query IR → capability diff → target
artifact + compatibility report.

Every finding carries a status: `exact`, `compatible`, `approximate`,
`unsupported`, or `requires_decision`. Profiles (`strict`, `compatible`,
`best-effort`) only decide which statuses abort the conversion. They never
change the reported status. Reports are deterministic and include the source
construct, the source span where available, the target representation, and the
reason.

## 9. Testing strategy

| Layer | Tooling | What it proves |
| --- | --- | --- |
| Unit | pytest | lexer, parser, AST, types, IR validation, normalization |
| Property | Hypothesis | round-trips, span mapping, parser invariants, type lattice |
| Golden | pytest fixtures | emitted SQL / documents / diagnostics are reviewed artifacts |
| Integration | pytest + Docker Compose | real execution against real engines |
| Security | pytest | parameter binding, policy enforcement, redaction, injection attempts |
| Packaging | pytest | namespace installability, version consistency, dependency direction |
| Benchmarks | pytest-benchmark | lexing/parsing/planning regressions |

Integration tests are marked `integration` and are excluded from the default
fast suite. CI runs them in a separate job once the first adapter exists.
Generated artifacts are checked into fixtures only after they have been
executed successfully, so a golden file is evidence, not a guess.

## 10. Current state (Phase 0)

Implemented and tested:

* workspace packaging and version consistency checks,
* source file model with CRLF/LF/CR and Unicode-correct positions,
* diagnostics model, bag, ordering, and renderer,
* normative language specification, grammar, and universal type-system contract,
* IR specification, adapter contract, CLI contract, security model,
  conversion and migration designs,
* test and documentation invariants.

Not implemented: lexer, parser, AST, semantic analysis, IR models, compiler,
adapters, runtime, CLI commands, SDKs, migration engine. See
`KNOWN_LIMITATIONS.md`.

## 11. Phase gates

Each phase ends with a written report, an exact test run, a list of created and
modified files, an explicit list of unimplemented items, security notes, and a
git commit when the tree is clean and tests pass. See
`IMPLEMENTATION_ROADMAP.md` for the gate criteria.
