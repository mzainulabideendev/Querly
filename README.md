# Querly

**One Language. Many Databases.**

Querly is a universal database programming language. You describe schemas,
queries, mutations, transactions, and migrations once, in an English-like or
structured syntax, and compile them to a database-independent IR. Adapters lower
that IR to real database engines, and the conversion engine reports exactly what
survives when you move between them.

```text
create table users
with:
    id as unique number
    name as required text
    email as required email
    age as optional number

get all users where age is greater than 18 sort by name ascending
```

```sql
table users {
    id: integer @primary @auto
    name: string @required
    email: email @required @unique
    age: integer?
}

query adults {
    from users
    select id, name, age
    where age > 18
    order by name asc
}
```

Both forms produce the same normalized IR.

## Project status

**Phase 0 (specification and repository foundation) is complete and awaiting
approval.** This repository does **not** yet contain a working compiler,
adapter, runtime, or CLI. Nothing here can connect to a database or execute a
query, and no database is supported.

| Area | Phase 0 state |
| --- | --- |
| Language specification and formal grammar | Written, normative draft (`0.1`) |
| IR, adapter, CLI, security, conversion, migration designs | Written |
| Workspace packaging, installability, versioning | Implemented and tested |
| Source model (encoding, offsets, CRLF/LF/CR, Unicode) | Implemented and tested |
| Diagnostics (codes, spans, bag, renderer) | Implemented and tested |
| Lexer, parser, AST, semantic analysis | Not started (Phase 1-3) |
| Adapters | Not started (Phase 4+) |
| Runtime, CLI, SDKs | Not started (Phase 7-9) |

The authoritative status is [`PROJECT_STATUS.md`](PROJECT_STATUS.md). The exact
test evidence is [`TEST_REPORT.md`](TEST_REPORT.md). Known gaps are listed in
[`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md), including language-level gaps
such as the absence of subqueries in version 0.1.

## Why another database language

Existing tools either target one engine, hide incompatibilities behind a
lowest-common-denominator API, or generate SQL without telling you what changed.
Querly is built around three rules:

1. **One meaning, two spellings.** Readable English-like syntax and precise
   structured syntax compile to the same IR.
2. **Capabilities, not claims.** An adapter declares what it supports and is
   tested against real database versions. Unsupported features fail early.
3. **No silent loss.** Conversions report `exact`, `compatible`, `approximate`,
   `unsupported`, or `requires_decision` per construct. Lossy conversion is
   never presented as lossless.

Parsing and compilation are deterministic and never depend on AI. An optional
assistant is planned, but its output is ordinary source text that must pass the
same compiler.

## Repository layout

```text
packages/       Python distributions sharing the `oq` namespace
  oq-core/      source model, diagnostics, lexer, parser, AST, semantics
  oq-ir/        versioned universal IR
  oq-types/     universal type system
  oq-compiler/  normalization, capability analysis, planning, emission
  oq-runtime/   connections, execution, transactions, policy
  oq-converter/ schema and query conversion
  oq-migrations/migration planning and verification
  oq-cli/       `oq` command
  oq-sdk-python/Python embedding API
  oq-ai/        optional assistant
adapters/       independently installable engine adapters (Phase 4+)
docs/           specifications and guides
tests/          cross-package invariants
```

## Development setup

Requirements: Python 3.12 or newer, Git, and [uv](https://docs.astral.sh/uv/).
Node.js 20+ is needed only once the JavaScript SDK exists.

```text
git clone <repository-url>
cd Querly
uv sync --all-packages
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Windows PowerShell, Linux, and macOS use the same commands. Integration tests
need Docker and are opt-in with `-m integration` once adapters exist.

## Documentation

| Document | Purpose |
| --- | --- |
| [`docs/language/SPECIFICATION.md`](docs/language/SPECIFICATION.md) | Normative language semantics |
| [`docs/language/grammar.ebnf`](docs/language/grammar.ebnf) | Formal grammar |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Pipeline, dependency rules, layer boundaries |
| [`docs/ir/IR_SPECIFICATION.md`](docs/ir/IR_SPECIFICATION.md) | Universal IR contract |
| [`docs/adapters/ADAPTER_GUIDE.md`](docs/adapters/ADAPTER_GUIDE.md) | Adapter contract and review checklist |
| [`docs/conversion/CONVERSION_GUIDE.md`](docs/conversion/CONVERSION_GUIDE.md) | Conversion workflows and statuses |
| [`docs/migrations/MIGRATION_GUIDE.md`](docs/migrations/MIGRATION_GUIDE.md) | Migration lifecycle and safety |
| [`docs/security/SECURITY.md`](docs/security/SECURITY.md) | Threat model and controls |
| [`docs/cli/CLI_REFERENCE.md`](docs/cli/CLI_REFERENCE.md) | Command and exit-code contract |
| [`docs/sdk/python.md`](docs/sdk/python.md) | Python SDK design |
| [`docs/sdk/javascript.md`](docs/sdk/javascript.md) | JavaScript SDK design |
| [`IMPLEMENTATION_ROADMAP.md`](IMPLEMENTATION_ROADMAP.md) | Phases and acceptance gates |
| [`DATABASE_COMPATIBILITY.md`](DATABASE_COMPATIBILITY.md) | Honest per-engine status |
| [`DECISIONS.md`](DECISIONS.md) | Architecture decision records |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Contribution workflow |

## License

MIT. See [`LICENSE`](LICENSE).

## Contributing

Contributions are welcome after the phase gate process is understood. Read
[`CONTRIBUTING.md`](CONTRIBUTING.md) and [`DECISIONS.md`](DECISIONS.md) first.
Do not add support claims for databases or features that are not tested.
