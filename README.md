# Querly

**One Language. Many Databases.**

Querly is a universal database programming language: you describe schemas,
queries, mutations, transactions, and migrations once, in an English-like or
structured syntax, and the toolchain is designed to compile them to a
database-independent IR, lower that IR to real engines through adapters, and
report exactly what survives a conversion.

```text
create table users
with:
    id as unique number
    name as required text
    email as required email
    age as optional number

get all users where age is greater than 18 sort by name ascending
```

```text
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

Both forms are specified to produce the same normalized IR.

> **Current status: pre-release `0.1.0.dev0`, not ready for production.**
> The repository is a working workspace of packages plus specifications. The
> `oq` CLI installs and runs, and the `oq-core` source, diagnostic, and lexer
> modules are implemented and tested. The grammar is not yet connected to the
> hand-written lexer, so `oq check` and `oq compile` currently return parse
> diagnostics for ordinary source files. Do not rely on compilation or database
> execution yet. Read [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) before
> evaluating Querly.

## Download and install

Every tagged release publishes a standalone `oq` executable for the common
desktop platforms.

1. Open the **Releases** page:
   <https://github.com/mzainulabideendev/Querly/releases/latest>.
2. Download the asset that matches your system:

   | Platform | Asset |
   | --- | --- |
   | Linux (x86_64) | `oq-linux-x86_64` |
   | macOS (Apple Silicon / arm64) | `oq-macos-arm64` |
   | macOS (Intel / x86_64) | `oq-macos-x86_64` |
   | Windows (x86_64) | `oq-windows-x86_64.exe` |

3. Make it executable (macOS and Linux only) and run it:

   ```bash
   chmod +x oq-linux-x86_64
   ./oq-linux-x86_64 version
   ```

   On macOS the first run may be blocked by Gatekeeper. Allow it under
   **System Settings → Privacy & Security**, or clear the quarantine flag:

   ```bash
   xattr -d com.apple.quarantine ./oq-macos-arm64
   ```

   On Windows, rename `oq-windows-x86_64.exe` to `oq.exe` and add its folder to
   `PATH` if you want to call `oq` from any directory.

4. Verify the download (optional). Every release also ships a `SHA256SUMS`
   manifest:

   ```bash
   sha256sum -c SHA256SUMS
   ```

The same release page carries wheel and source distributions for each Python
package and adapter, for embedding Querly in a Python application.

## Use the CLI

The `oq` executable requires no Python installation.

```bash
oq version     # prints the release version, e.g. 0.1.0.dev0
oq --help
```

The command surface planned for the compiler is:

```bash
oq check example.oq              # syntax and semantic diagnostics
oq check example.oq --strict     # treat warnings as errors
oq check example.oq --json       # machine-readable diagnostics
oq compile example.oq --dialect sqlite
oq compile example.oq --dialect postgres --json
```

`check` and `compile` are wired to the parser, which is still being connected to
the hand-written lexer. Until that work lands they report parse diagnostics for
real programs, so treat their output as experimental. Exit codes are stable:
`0` for success, `1` for diagnostics or compilation errors, and `2` for usage or
file errors.

## Python API

Python 3.12 or newer is required. The workspace is not published on PyPI yet;
install from a source checkout or from the wheels attached to a release.

```bash
# from a checkout
uv sync --all-packages
```

The stable, usable parts of `oq-core` today are the source model, diagnostics,
and the lexer:

```python
from oq.core.source import SourceFile
from oq.core.tokens import Lexer

source = SourceFile.from_text("table users { id: integer @primary }")
result = Lexer(source).tokenize()

for token in result.tokens:
    print(token.kind, token.text, token.span)
```

The parser, semantic analysis, compiler, runtime, converter, migrations, SDK, and
adapters are installable scaffolds; their `__init__` modules document that, and
they export no stable API yet.

### Adapter drivers

Adapter drivers are optional so the core stays lightweight. MariaDB's driver
builds native code, so it is only installed on request:

```bash
uv pip install -e "adapters/oq-adapter-mariadb[driver]"
```

## Supported databases

Adapter packages exist for all of the engines below. They are scaffolding at
this stage: the capability reports and conversion rules are being filled in, and
none of them should be used against production data. The authoritative,
per-engine truth is [`DATABASE_COMPATIBILITY.md`](DATABASE_COMPATIBILITY.md).

| Engine | Package |
| --- | --- |
| SQLite | `oq-adapter-sqlite` |
| PostgreSQL | `oq-adapter-postgres` |
| MySQL / MariaDB | `oq-adapter-mysql`, `oq-adapter-mariadb` |
| MongoDB / DocumentDB | `oq-adapter-mongodb`, `oq-adapter-documentdb` |
| Redis | `oq-adapter-redis` |
| Cassandra / ScyllaDB | `oq-adapter-cassandra`, `oq-adapter-scylladb` |
| Additional engines | `adapters/*` |

## Design rules

The project is built around three rules:

1. **One meaning, two spellings.** Readable English-like syntax and precise
   structured syntax compile to the same IR.
2. **Capabilities, not claims.** An adapter declares what it supports and is
   tested against real database versions. Unsupported features fail early.
3. **No silent loss.** Conversions report `exact`, `compatible`, `approximate`,
   `unsupported`, or `requires_decision` per construct. Lossy conversion is never
   presented as lossless.

Parsing and compilation are deterministic and never depend on AI. The optional
assistant's output is ordinary source text that must pass the same compiler.

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
adapters/       independently installable engine adapters
docs/           specifications and guides
tests/          cross-package invariants
tools/          development and release helpers
```

## Development setup

Requirements: Python 3.12 or newer, Git, and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repository-url>
cd Querly
uv sync --all-packages
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Run the CLI from the workspace with `uv run oq version`. Integration tests need
Docker and are opt-in:

```bash
uv run pytest -m integration
```

## Building a release

Runs of `.github/workflows/release.yml` produce everything users download. It is
triggered by pushing a `v*` tag or by manual dispatch. The workflow:

- builds wheel and source distributions for every package and adapter;
- builds standalone `oq` executables for Linux x86_64, macOS Intel, macOS Apple
  Silicon, and Windows x86_64;
- uploads all files plus a `SHA256SUMS` manifest to the GitHub release.

To build the Python distributions locally:

```bash
uv sync --frozen --all-packages
mkdir -p dist
for pkg in packages/*/ adapters/*/; do
  uv build --package "$(basename "$pkg")" --out-dir dist
done
```

To build a local CLI executable (requires PyInstaller):

```bash
uv sync --frozen --package oq-cli
uv pip install pyinstaller
uv run --no-sync pyinstaller --onefile --name oq \
  --collect-data oq.core tools/oq_launcher.py
./dist/oq version
```

`--collect-data oq.core` bundles the `.lark` grammar files, which the parser
loads from the package directory at runtime.

## Documentation

| Document | Purpose |
| --- | --- |
| [`docs/language/SPECIFICATION.md`](docs/language/SPECIFICATION.md) | Normative language semantics |
| [`docs/language/grammar.ebnf`](docs/language/grammar.ebnf) | Formal grammar |
| [`docs/types/TYPE_SYSTEM.md`](docs/types/TYPE_SYSTEM.md) | Universal type-system contract |
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
| [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) | What Querly does not do |
| [`DECISIONS.md`](DECISIONS.md) | Architecture decision records |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Contribution workflow |

## License

MIT. See [`LICENSE`](LICENSE).

## Contributing

Contributions are welcome. Read [`CONTRIBUTING.md`](CONTRIBUTING.md) and
[`DECISIONS.md`](DECISIONS.md) first. Do not add support claims for databases or
features that are not tested.
