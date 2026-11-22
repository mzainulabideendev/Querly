# Querly CLI Reference

**Command:** `oq`
**Status:** Phase 0 contract. The CLI is implemented in Phase 7. No command
below works yet, and the `oq` script is intentionally not installed until it
does something real.

## 1. Conventions

* `oq <command> [options] [arguments]`
* Exit codes:
  * `0` success,
  * `1` compilation or validation failure (`check`, `compile`, `convert`),
  * `2` usage error (unknown command, bad flags, missing file),
  * `3` connection failure,
  * `4` execution failure,
  * `5` policy refusal (read-only violation, unconfirmed destructive action),
  * `6` capability failure (requested operation unsupported by the adapter).
* Human output goes to stdout for results and stderr for diagnostics. `--json`
  emits a single machine-readable document on stdout and suppresses decorative
  output.
* Credentials come from the environment, a secret provider, or a prompt. They
  are never accepted as a command-line flag that could leak into shell history.
* `--dry-run` prints the artifacts and plan without executing. It never claims
  execution.

## 2. Commands

### `oq init`

Initialize a project directory.

```text
oq init [path] [--name NAME] [--adapter ADAPTER]
```

Creates `querly.toml`, a `schema.oq` starter, a `queries/` directory, and a
`.gitignore` entry for local state. Refuses to overwrite existing files unless
`--force` is given.

### `oq check`

Parse, analyze, and type-check source files. No database connection.

```text
oq check [paths...] [--strict] [--json]
```

Exit `0` only when there are no errors. Warnings are non-fatal unless
`--strict`. Prints diagnostics with source snippets.

### `oq compile`

Compile source to universal IR and optionally to a target artifact. Does not
execute.

```text
oq compile <path> [--to IR_FILE] [--adapter ADAPTER] [--out DIR] [--json]
```

Without `--adapter`, writes versioned IR JSON. With an adapter, performs
capability analysis and writes artifacts plus a compatibility report. A
successful compile is not execution and the output says so.

### `oq explain`

Explain a query plan without executing it.

```text
oq explain <query> [--adapter ADAPTER] [--connection NAME] [--json]
```

Shows the normalized IR, required capabilities, the generated artifact with
parameters as placeholders, safety classification, and estimated limits. If the
adapter supports native explain plans, the adapter may include them, clearly
labeled as engine output.

### `oq run`

Execute a query or mutation against a configured connection.

```text
oq run <query> [--connection NAME] [--param NAME=VALUE ...] [--json]
               [--allow-unfiltered] [--limit N] [--timeout SECONDS]
```

* Mutations require `--allow-unfiltered` when they have no filter.
* Destructive operations require an interactive confirmation or
  `--yes-destructive`.
* Read-only connections reject writes before execution.
* Output includes affected row counts for mutations and column metadata for
  reads.
* `--json` output distinguishes `generated`, `executed`, and `result`.

`VALUE` parsing supports strings, integers, decimals, booleans, null, and JSON
values. Values are bound as parameters, never interpolated.

### `oq connect`

Manage saved connection profiles.

```text
oq connect add NAME --adapter ADAPTER [--host H] [--port P] [--database DB]
                    [--user U] [--sslmode MODE]
oq connect test NAME
oq connect list
oq connect remove NAME
```

Passwords are read from a secret provider or prompted, never from flags.
Profiles store non-secret fields only. Profiles are validated with a real
connection test when `oq connect test` runs.

### `oq inspect`

Inspect an existing database into universal schema IR.

```text
oq inspect --connection NAME [--entity NAME...] [--out FILE] [--json]
```

Read-only. Reports unsupported objects separately. Never writes to the source
database.

### `oq convert`

Convert a schema or query between engines.

```text
oq convert schema --connection NAME --to ADAPTER [--profile PROFILE]
                  [--entities A,B] [--out DIR] [--json]
oq convert query <path> --to ADAPTER [--profile PROFILE] [--json]
```

Profiles: `strict`, `compatible` (default), `best-effort`. Always emits a
compatibility report. Exit `6` when the profile rejects the conversion.

### `oq migrate`

Plan, verify, or apply migrations.

```text
oq migrate plan <migration> --connection NAME [--json]
oq migrate apply <migration> --connection NAME [--yes-destructive] [--json]
oq migrate status --connection NAME [--json]
oq migrate verify <migration> --connection NAME [--json]
```

`plan` and `verify` are read-only. `apply` requires either transactional DDL or
documented manual recovery steps, and requires `--yes-destructive` for
data-loss actions. A checkpoint is written only after verified success.

### `oq adapters`

List installed adapters and capabilities.

```text
oq adapters list [--json]
oq adapters info ADAPTER [--json]
oq adapters test ADAPTER [--connection NAME] [--json]
```

An adapter absent from the list is not supported. `adapters test` runs the
adapter's self-check against a configured connection and reports tested engine
versions.

### `oq test`

Run the project's Querly test files (assertions over schema and queries).
Introduced with the SDK in Phase 9. Until then the command reports that the
feature is unavailable with exit `2`; it must not pretend to pass.

### `oq version`

Print versions without connecting anywhere.

```text
oq version [--json]
```

Reports CLI version, language version, IR format version, installed adapters,
and their versions.

## 3. Configuration file

`querly.toml` (Phase 7):

```toml
[project]
name = "billing"
language_version = "0.1"

[connections.dev]
adapter = "sqlite"
path = "./dev.db"
read_only = true

[connections.staging]
adapter = "postgres"
host = "localhost"
port = 5432
database = "billing"
user = "billing_app"
sslmode = "require"

[policy]
read_only = false
max_rows = 10000
timeout_seconds = 30
confirm_destructive = true
```

Secrets are referenced, not stored:

```toml
[connections.staging]
password_env = "BILLING_DB_PASSWORD"
```

## 4. Windows, Linux, and macOS

The CLI is a Python console script and behaves identically on all three. Paths
are handled with `pathlib`, output encoding is UTF-8, and ANSI color is disabled
when `NO_COLOR` is set or stdout is not a terminal. PowerShell examples use the
same `oq` invocation as bash and zsh.
