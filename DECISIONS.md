# Architecture Decision Records

Decisions are recorded in the order they were made. Status values: `accepted`,
`open`, `superseded`. A decision marked `open` must be resolved before the phase
that depends on it.

## ADR-0001 — Project name and stable identifiers

**Status:** accepted

**Context.** The originating brief calls the product both "OmniQuery" and
"Querly", and names the CLI `oq`.

**Decision.** The product name is **Querly**. The tagline is "One Language.
Many Databases." The CLI is `oq`. The Python namespace is `oq`. Distribution
names use the `oq-` prefix (`oq-core`, `oq-cli`, ...). Adapter distributions use
`oq-adapter-<engine>` and the `oq_adapters` namespace.

**Consequences.** Documentation uses Querly consistently. "OmniQuery" appears
only as historical context. Renaming later would break imports and entry points,
so this requires a major release.

## ADR-0002 — Python 3.12+, namespace packages, uv workspace, hatchling

**Status:** accepted

**Context.** The brief requires a modular monorepo with separately installable
components.

**Decision.** Python 3.12 is the minimum. All components share the PEP 420
namespace `oq` with no `oq/__init__.py`, so each distribution contributes a
subpackage without shadowing others. Each package is built by hatchling. The
workspace is managed by uv with `tool.uv.workspace` and explicit workspace
sources. A root test asserts every package's version matches `oq.core.version`.

**Consequences.** Installing any subset works. A missing top-level `__init__` is
intentional and tested. Contributors need uv for the documented workflow, but
plain pip editable installs remain possible.

## ADR-0003 — Two surface syntaxes, one AST and one IR

**Status:** accepted

**Context.** The brief requires English-like and structured forms with identical
meaning.

**Decision.** Lexing and parsing accept both forms. They normalize to the same
canonical AST shape and IR. English-like declarations are line-oriented where a
line is the natural terminator (schema fields, mutations); elsewhere clauses
start with keywords, so newlines are optional. Mixing forms inside one
declaration is an error. Optionality defaults to required in both forms.

**Consequences.** The parser has a `newline` token that is significant only in
English-like constructs. Diagnostics must explain mixed-form errors clearly.
Grammar tests enforce that both forms remain expressible.

## ADR-0004 — Canonical universal types; no engine names in the IR

**Status:** accepted

**Context.** Types differ substantially between engines, and leaking engine type
names into the IR would make honest conversion impossible.

**Decision.** The IR uses a fixed canonical type set (`integer`, `decimal(p,s)`,
`text(n)`, `boolean`, `date`, `time`, `timestamp`, `duration`, `uuid`, `email`,
`url`, `json`, `binary(n)`, `geo`, `list`, `set`, `map`, `vector(n)`,
`reference`). Aliases are normalized away. Nullability is an attribute property,
not a type property. Adapter-specific types appear only in capability reports and
physical artifacts.

**Consequences.** Some engines will map several universal types to one native
type; that is acceptable and reported. Some universal types will be unsupported
on some engines; that is reported, never approximated silently.

## ADR-0005 — Versioned JSON IR with validated models

**Status:** accepted

**Context.** The IR is shared by the compiler, converters, adapters, SDKs, and
third-party tools.

**Decision.** IR documents are JSON with an envelope containing `format`,
`format_version`, `language_version`, and `generator`. Models are validated
Pydantic types (Phase 2). Unknown fields are preserved by round-tripping tools.
Breaking changes require a major format version. No credentials, hostnames, or
engine SQL appear in portable IR.

**Consequences.** The IR is an inspectable public contract. Serialization is
deterministic for golden tests. Binary encodings are deferred.

## ADR-0006 — Adapter contract with machine-readable capabilities and entry points

**Status:** accepted

**Context.** The project must support many engines without advertising unverified
support.

**Decision.** Adapters implement a protocol covering connection, inspection,
schema and query compilation, parameterized execution, transactions, capability
reporting, result and error normalization, and version detection. Discovery uses
the `oq.adapters` entry point group. Capability reports are versioned and list
only CI-tested engine versions. Core packages never import a driver.

**Consequences.** Third-party adapters are possible without modifying the core.
An adapter that cannot bind parameters cannot run parameterized queries. A
capability marked supported without a test is a defect.

## ADR-0007 — Safety enforced by the runtime from plan metadata, not SQL inspection

**Status:** accepted

**Context.** Scanning generated SQL for `DELETE` is fragile and fails across
dialects and pipelines.

**Decision.** Every plan declares a safety classification (`read`, `write`,
`destructive_write`, `ddl`), an unfiltered flag, and a destructive flag. The
runtime enforces read-only mode, confirmation, row limits, and timeouts before
execution. Values are always bound as parameters; the language has no
interpolation operator.

**Consequences.** Adapters must classify accurately; a wrong classification is a
security defect. Policy refusal happens before any database work.

## ADR-0008 — Migration checksums, verification, and no automatic retry

**Status:** accepted

**Context.** Partial migrations and edited applied migrations are common causes of
schema drift.

**Decision.** Checkpoints store the compiled-IR checksum, status, actor, engine
version, and timestamps. Applied migrations are immutable; editing one is an
error. Post-conditions are verified by re-inspection before a success checkpoint
is written. Failures mark the migration failed and never retry automatically.
Non-transactional DDL requires explicit acknowledgement and documented recovery.

**Consequences.** Some engines cannot offer atomic migrations; the tool says so
instead of implying safety. Reconciliation requires an explicit human action.

## ADR-0009 — Diagnostic code format, code-point offsets, and line-break rules

**Status:** accepted

**Context.** Diagnostics must be stable for tooling and accurate on Windows and
Unix files.

**Decision.** Codes match `OQ-AREA-NNN` with an uppercase area of 2-6 letters.
Offsets are zero-based Unicode code points; lines and columns are one-based;
spans are half-open. The only line breaks are `\r\n`, `\r`, and `\n`. A run of
line breaks is one `newline` token. NUL bytes are rejected; a UTF-8 BOM is
stripped. The renderer prints a caret span and notes, in a compiler-style format.

**Consequences.** Positions are reproducible across platforms. Tools must not
assume byte offsets. The source model and diagnostics renderer share one
line-break definition to prevent drift.

## ADR-0010 — Conversion status vocabulary and profiles

**Status:** accepted

**Context.** "Lossless conversion" claims are easy to make and hard to prove.

**Decision.** Findings use exactly `exact`, `compatible`, `approximate`,
`unsupported`, `requires_decision`. Profiles (`strict`, `compatible`,
`best-effort`) only choose which statuses abort; they never rewrite the reported
status. `exact` requires a proof obligation. Dropped constraints are never
`compatible`. `unsupported` aborts every profile, including `best-effort`.

**Consequences.** Reports are comparable across engines. Users must make
decisions explicitly. The converter cannot be configured to hide loss.

## ADR-0011 — Phase gates with evidence, and no premature support claims

**Status:** accepted

**Context.** The brief forbids prototypes presented as products and requires
verification at every phase.

**Decision.** Each phase ends with a report containing exact test commands and
results, created/modified files, unimplemented functionality, security notes, and
regression verification. A phase does not start until the previous one is
approved. The compatibility matrix lists only tested support. Scaffold packages
state that they are scaffolds in their docstrings and README.

**Consequences.** Progress is slower but auditable. Documentation and behavior
cannot diverge silently because tests check document presence and assertions.

## ADR-0012 — JavaScript SDK bridge

**Status:** open

**Context.** The compiler is Python. A JavaScript SDK needs either a process
bridge, a local sidecar, a WASM build of Python, or a second implementation.
A second implementation is rejected because it guarantees divergence.

**Decision (pending).** Phase 9 must choose among CLI process, loopback sidecar,
and WASM after benchmarking startup, memory, and packaging size, and after a
security review of the bridge. No npm package will be published before then.

**Consequences.** The JavaScript SDK cannot be promised earlier than Phase 9. The
documentation describes the constraint rather than guessing an API transport.
