# Querly Python SDK

**Status:** Phase 0 design. Implemented in Phase 9. The package `oq-sdk-python`
exists today only as an installable scaffold with no public API.

## 1. Goals

* Embed the full pipeline: check, compile, convert, inspect, execute, migrate.
* Keep library APIs structured and silent; the SDK returns values and
  diagnostics, and never prints or exits.
* Never hide the difference between compiled output and executed operations.
* Be usable from applications, notebooks, and the test suite.

## 2. Planned public surface

```python
from oq.sdk import Querly, ConnectOptions, ExecutionPolicy

querly = Querly.from_project(".")

checked = querly.check("schema.oq", strict=True)
if not checked.ok:
    for diagnostic in checked.diagnostics:
        print(diagnostic.render(checked.source_of(diagnostic)))

compiled = querly.compile("queries/adults.oq", adapter="sqlite")
print(compiled.artifact.text)  # generated, not executed
print(compiled.plan.safety.classification)

connection = querly.connect(
    "dev",
    policy=ExecutionPolicy(read_only=True, max_rows=1_000, timeout_seconds=10),
)
result = connection.execute(compiled, params={"min_age": 18})
print(result.columns, result.rows, result.affected_rows)
```

Inspection and conversion:

```python
schema = connection.inspect(entities=["users", "posts"])
report = querly.convert_schema(schema, to="postgres", profile="compatible")
print(report.status)
for finding in report.findings:
    print(finding.status, finding.construct, finding.reason)
```

Migrations:

```python
plan = querly.migrate.plan("migrations/add_email.oq", connection=connection)
assert plan.data_loss_risk is False
querly.migrate.apply(plan, acknowledge_destructive=False)
```

## 3. Design rules

1. **No implicit I/O.** Constructing a compiler or converter performs no network
   or database access. Connections are explicit.
2. **Typed results.** Every result is a dataclass or Pydantic model with stable
   field names. Exceptions are reserved for programmer errors and policy
   refusals, not for user diagnostics.
3. **Diagnostics are data.** A failed check returns diagnostics plus the source
   they refer to, so callers can render them however they want.
4. **Execution requires a plan.** `execute` accepts a compiled plan, or compiles
   and executes in one call only through an explicitly named convenience method.
5. **Safety is opt-out, never opt-in.** Read-only defaults to the project
   configuration; destructive operations require an explicit acknowledgement
   argument.
6. **No hidden retries.** Retry policies are explicit connection options and
   never apply to writes unless the caller declares an idempotency key.
7. **Redaction.** `repr` of connection objects, plans, and errors never includes
   secrets or bound values.

## 4. Package layout (planned)

```text
oq-sdk-python/
  src/oq/sdk/
    __init__.py        public exports
    project.py         project discovery and configuration
    compile.py         check/compile orchestration
    connect.py         connection factory and policy
    convert.py         conversion facade
    migrate.py         migration facade
    errors.py          SDK exceptions
```

The SDK orchestrates; it does not reimplement the compiler. All real work lives
in the underlying packages.

## 5. Compatibility

The SDK follows semantic versioning from 1.0. Before 1.0, minor versions may
change APIs with a changelog entry. The IR format has its own version, documented
in `docs/ir/IR_SPECIFICATION.md`.

## 6. Testing obligations

* Public API examples in this document are executable tests once implemented.
* Type checking of the public API with `mypy --strict` for consumer-style usage.
* Every diagnostic path has a test.
* Redaction tests assert that secrets and bound values cannot appear in `repr`,
  exception messages, or logs.
* Execution tests run against real adapters and are marked `integration`.
