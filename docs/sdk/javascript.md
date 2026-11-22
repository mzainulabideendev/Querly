# Querly JavaScript / TypeScript SDK

**Status:** Phase 0 design. Implemented in Phase 9. Nothing is published to npm
and no JavaScript package exists yet.

## 1. Goals

* Let Node.js, Bun, and browser-adjacent tooling check, compile, and convert
  Querly programs.
* Support execution in Node.js and Bun through real adapters, without
  reimplementing adapters in JavaScript.
* Ship first-class TypeScript types for IR documents, diagnostics, plans, and
  results.
* Preserve the same safety rules as the Python SDK: no implicit I/O, no hidden
  execution, no credential leakage.

## 2. Distribution decision (open)

The compiler is implemented in Python. A JavaScript SDK therefore needs a
bridge. The options are:

| Option | Pros | Cons |
| --- | --- | --- |
| Spawn the `oq` CLI per operation | simple, always current, no duplicate compiler | process overhead, requires Python toolchain |
| Long-lived local sidecar process | fast, one toolchain, streaming | lifecycle management, port security |
| Compile Python to WASM (Pyodide or similar) | runs in browser and Node | large download, driver limits, partial filesystem |
| Reimplement the front end in TypeScript | native performance | two compilers, guaranteed divergence — rejected |

This decision is **not made**. It is recorded as an open ADR and must be
resolved in Phase 9 with benchmarks and a security review before any package is
published. The SDK will not ship a partial reimplementation of the compiler.

## 3. Planned surface (subject to the decision above)

```ts
import { Querly } from "@querly/sdk";

const querly = await Querly.fromProject(".");
const checked = await querly.check("schema.oq");
if (!checked.ok) {
  for (const d of checked.diagnostics) console.error(d.message, d.span);
}

const compiled = await querly.compile("queries/adults.oq", { adapter: "sqlite" });
console.log(compiled.artifact.text); // generated, not executed

const connection = await querly.connect("dev", {
  policy: { readOnly: true, maxRows: 1000, timeoutSeconds: 10 },
});
const result = await connection.execute(compiled, { minAge: 18 });
console.log(result.rows, result.affectedRows);
```

Type sketch:

```ts
export type Severity = "error" | "warning" | "info" | "hint";

export interface Diagnostic {
  code: string;
  severity: Severity;
  message: string;
  span?: { file: string; start: Position; end: Position };
  notes: string[];
}

export interface CompiledQuery {
  kind: "compiled"; // never "executed"
  adapter: string;
  artifact: { kind: "sql" | "pipeline" | "command"; text: string };
  parameters: { name: string; type: TypeIR }[];
  safety: { classification: "read" | "write" | "destructive_write" | "ddl" };
}

export interface ExecutionResult {
  kind: "result";
  columns: { name: string; type: TypeIR }[];
  rows: unknown[];
  affectedRows: number | null;
}
```

## 4. Packaging

* Package name: `@querly/sdk`.
* ESM first, with a CommonJS build only if a concrete consumer needs it.
* Published with TypeScript declaration files and source maps.
* Node.js 20+ and Bun are the supported execution runtimes. Browser support
  depends on the bridge decision and is not claimed.

## 5. Security rules

* Secrets are read from environment variables or a provided secret provider,
  never from browser storage by default.
* A sidecar bridge binds to loopback only, authenticates with a per-process
  token, and refuses arbitrary command execution.
* `JSON.stringify` of connection options and errors must not contain secrets or
  bound values.
* Execution requires an explicit connection; there is no ambient default.

## 6. Testing obligations

* Type-level tests with `tsc --noEmit` on the public examples.
* Contract tests against the same IR fixtures used by the Python SDK.
* Error-path tests for diagnostics, policy refusals, and unsupported
  capabilities.
* Integration tests in Node.js and Bun against real adapters once the bridge
  exists.
* No test may assert that a generated artifact was executed without an
  execution result.
