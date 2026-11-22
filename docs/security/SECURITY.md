# Querly Security Model

**Status:** Phase 0 design. Every control below is a requirement for the phases
that implement it; none is implemented yet. This document is deliberately
specific so implementation can be tested against it.

## 1. Assets and threats

| Asset | Threat |
| --- | --- |
| Database credentials | leakage through logs, config, process arguments, error messages, crash reports |
| Tenant/user data | SQL injection, over-broad reads, accidental mass updates or deletes |
| Schema integrity | unverified or partially applied migrations |
| Audit trail | tampering, missing entries, storing sensitive values |
| Conversion output | silently lossy conversion that changes application semantics |
| Supply chain | malicious adapter, driver, or plugin |

Querly's core defence is structural: values are never part of the language's
generated text. Identifiers are resolved to compiled declarations, not built
from user input. There is no string interpolation operator and no raw-SQL escape
hatch in language version 0.1.

## 2. Parameter binding

1. Query IR represents values as `literal` or `parameter` nodes.
2. Compilation replaces parameters with engine placeholders and returns a
   separate ordered binding plan.
3. The runtime binds values through the driver's parameter API.
4. An adapter that cannot bind parameters must refuse queries with parameters
   rather than concatenate them.
5. Tests must include adversarial values (`'; DROP TABLE users; --`, Unicode
   lookalikes, embedded NULs, overlong strings) and prove the artifact is
   unchanged and the value arrives as data.

`oq inspect` and generated DDL are the only paths that emit identifiers. Both
validate identifiers against a compiled schema and the adapter's quoting rules.

## 3. Credentials and connection security

* Configuration stores references (`password_env`, secret provider key), never
  plaintext passwords.
* Passwords are never accepted as CLI flags. They are prompted without echo or
  read from the environment/secret provider.
* Connection strings are redacted before logging, including userinfo in URLs and
  query parameters such as `password=` and `sslkey=`.
* `repr()` of connection configuration redacts secret fields by construction.
* TLS is negotiated by the driver. The adapter reports whether the connection is
  encrypted and fails closed when the profile requires TLS and the server does
  not provide it.
* Connection timeouts and statement timeouts have finite defaults. There is no
  default of "unlimited".

## 4. Policy enforcement

Policy is evaluated by the runtime before execution, using the plan's declared
safety classification. It is never inferred by scanning generated SQL.

| Policy | Effect |
| --- | --- |
| read-only | rejects `write`, `destructive_write`, and `ddl` plans |
| require confirmation | destructive plans need an explicit acknowledgement token |
| unfiltered writes | updates/deletes with no filter need `--allow-unfiltered` |
| max rows | result sets above the limit raise an error, never truncate silently |
| timeout | statement is cancelled; timeout error is reported as such |
| allowed adapters | plans for other adapters are rejected |
| trusted targets | optional allowlist of host/database pairs |

Native database permissions remain the primary control. Querly policy is
defence in depth, not a replacement for least-privilege database roles. The
documentation recommends a read-only role for inspection and a narrowly scoped
write role for mutations.

## 5. Destructive operations

Destructive operations include dropping a table or column, truncating data,
unfiltered updates/deletes, and migrations with `data_loss_risk: true`.

Requirements:

* The compiler marks the plan, the CLI prints what will be destroyed, and the
  runtime refuses without an acknowledgement.
* Acknowledgement is scoped to one plan or one migration run. It is not a global
  setting.
* Non-interactive environments must pass an explicit flag and are logged as
  non-interactive.
* Read-only mode always wins over acknowledgement.

## 6. Migrations

* `oq migrate plan` is read-only and shows forward actions, irreversibility, and
  data-loss risk.
* `apply` verifies the inspected schema against the expected pre-state before
  executing. A mismatch aborts.
* Migrations run in a transaction when the adapter reports transactional DDL.
  When it does not, the planner marks the migration as non-atomic and requires
  acknowledgement; it never claims atomicity.
* A checkpoint is recorded only after post-conditions are verified.
* A failed migration leaves an explicit status record and does not silently
  retry on the next run.

## 7. Audit logging

Audit records contain: timestamp, actor (local OS user or configured service
identity), adapter id, target database (host omitted or hashed if configured),
operation classification, IR query name, parameter names (not values), row
count, duration, outcome, and correlation id.

Audit records never contain: credentials, full connection strings, bound values,
or complete result rows. Values may be hashed for correlation only when a policy
explicitly enables it.

Logs are structured (JSON lines) in non-interactive mode and are written to a
configurable destination. Writing to a database is opt-in and uses a separate
connection.

## 8. Conversion safety

Conversion reports are security-relevant because a silently weakened constraint
can become a data-integrity incident. Requirements are in
[`CONVERSION_GUIDE.md`](CONVERSION_GUIDE.md): no `exact` status without proof,
explicit `loss_of_constraints`, and profile-based refusal.

## 9. Supply chain

* Core packages have no third-party runtime dependencies except validated
  schema libraries.
* Adapters and drivers are pinned and their versions recorded in the capability
  report.
* Adapter discovery loads only entry points in the documented group and does not
  execute code from the inspected database.
* Generated artifacts are never executed as shell commands.
* CI builds from a lockfile and publishes provenance for releases.

## 10. Reporting a vulnerability

Report suspected vulnerabilities privately to the maintainers listed in the
repository before opening a public issue. Do not include credentials or
production data in a report; redact and describe.

## 11. Implementation status

| Control | Status |
| --- | --- |
| Parameter binding | designed, implemented in Phase 7 |
| Credential redaction | designed, implemented in Phase 7 |
| Read-only and confirmation policy | designed, implemented in Phase 7 |
| Timeouts, cancellation, row limits | designed, implemented in Phase 7 |
| Audit logging | designed, implemented in Phase 7 |
| Migration verification and checkpoints | designed, implemented in Phase 6/7 |
| Secret providers | designed, implemented in Phase 7 |
| Adversarial injection tests | planned with the first adapter (Phase 4) |
