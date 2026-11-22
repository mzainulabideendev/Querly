# Querly Migration Guide

**Status:** Phase 0 design. The migration engine is implemented in Phase 6,
with secure execution in Phase 7. Nothing can be migrated today.

## 1. Model

A migration is a versioned, ordered change set with an `up` block and an
optional `down` block. It is compiled to universal migration IR, planned against
the inspected schema, reviewed, and then executed exactly once.

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

Properties computed by the planner:

* `reversible` — a `down` block exists and every action in it is expressible.
* `data_loss_risk` — an action can remove data that the inspected schema shows
  may exist.
* `requires_transactional_ddl` — the target adapter reports `transactional_ddl:
  false`, so the plan is non-atomic.
* `lock_risk` — an action is known to require a table rewrite or an exclusive
  lock on the target engine.

## 2. Lifecycle

```text
oq migrate plan    -> inspect current schema, compile migration, show changes
oq migrate verify  -> compare expected pre-state and post-conditions, no writes
oq migrate apply   -> execute in a transaction/with recovery steps, record checkpoint
oq migrate status  -> show applied versions, checksums, pending migrations, failures
```

Checksums cover the compiled migration IR, not the source text, so formatting
changes do not create false drift. Editing an applied migration is detected and
reported as an error.

## 3. Planning rules

* The migration version must be greater than every applied version for the
  target database.
* Every action must be valid against the inspected schema. Adding a column that
  already exists, dropping a missing column, or changing a type the engine
  cannot convert is an error before any write.
* Adding a required column to a populated table requires a default or a
  multi-step plan. The planner refuses a single-step change that cannot succeed.
* Renames are explicit (`rename column old to new`). The planner never guesses
  that a drop plus an add was a rename.
* Destructive actions require acknowledgement. Dropping an empty column still
  requires acknowledgement; emptiness is discovered by inspection, not assumed.
* Type changes that narrow values are reported with a sample-based risk warning
  when the adapter can count violating rows cheaply. The planner does not
  rewrite data silently.

## 4. Execution and atomicity

* If the adapter reports transactional DDL, all statements of one migration run
  in one transaction. Failure rolls back and no checkpoint is recorded.
* If it does not, the planner emits ordered steps with a documented recovery
  procedure. The CLI requires explicit acknowledgement and prints the recovery
  steps before executing.
* Migrations are executed through the runtime, so read-only policy, timeouts,
  audit logging, and confirmation apply.
* Concurrent migrators are prevented by an engine-level advisory lock where
  available, or by a checkpoint row with a unique constraint. If neither is
  available, the planner reports that concurrent execution is unsafe.
* A partially applied migration records status `failed` with the last successful
  step and the error. It is never retried automatically.
* `down` executes only when the target state matches the migration's post-state.
  Rolling back through a chain is explicit (`oq migrate down --to VERSION`) and
  follows the same confirmation rules.

## 5. Checkpoints

A checkpoint row stores: migration name, version, compiled-IR checksum, status
(`applied`, `failed`, `rolled_back`), actor, start and finish timestamps,
target adapter and engine version, and correlation id. It never stores
credentials or data values.

Checkpoints live in a dedicated table managed by Querly. The table name is
configurable to avoid collisions. Creating it is itself a reported,
acknowledged migration step.

## 6. Verification

After executing a migration, the engine inspects the target schema again and
compares the result with the expected post-state IR. A checkpoint is written
only when the comparison matches, ignoring documented engine-specific
differences (for example default index names). A mismatch marks the migration
`failed` and keeps the data changes, because inventing a rollback would be
worse; the report explains the inconsistency and the recovery options.

## 7. Safety summary

| Situation | Behaviour |
| --- | --- |
| Read-only connection | migration rejected before execution |
| Missing acknowledgement for data loss | rejected |
| Target schema drift | rejected before any write |
| Concurrent migrator | blocked or reported unsafe |
| Non-atomic DDL | explicit warning plus recovery steps |
| Failed mid-migration (atomic) | rolled back, no checkpoint |
| Failed mid-migration (non-atomic) | `failed` checkpoint, manual steps, no auto-retry |
| Applied migration edited | error with checksum mismatch |

## 8. Out of scope for version 0.1

* Data backfills expressed in Querly (planned later as explicit data operations).
* Online/zero-downtime migration orchestration.
* Cross-database data migration. Schema conversion produces DDL and a plan; data
  movement is a separate tool.
* Automatic rollback of non-transactional DDL.
