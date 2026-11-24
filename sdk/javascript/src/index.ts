import { spawnSync } from "node:child_process";

export type Severity = "error" | "warning" | "info" | "hint";

export interface Diagnostic {
  code: string;
  severity: Severity;
  message: string;
  span?: { start: { line: number; column: number }; end: { line: number; column: number } };
  notes: string[];
}

export interface CheckResult {
  ok: boolean;
  diagnostics: Diagnostic[];
}

export interface CompileResult {
  ok: boolean;
  artifact: { kind: "sql"; dialect: string; text: string };
  diagnostics: Diagnostic[];
}

export interface QuerlyOptions {
  executable?: string;
  cwd?: string;
}

function run(executable: string, args: string[], cwd?: string): { status: number; stdout: string; stderr: string } {
  const result = spawnSync(executable, args, {
    cwd,
    encoding: "utf8",
    env: { ...process.env },
  });
  return { status: result.status ?? 1, stdout: result.stdout ?? "", stderr: result.stderr ?? "" };
}

export class Querly {
  private constructor(private readonly options: QuerlyOptions) {}

  static async fromProject(project: string, options: QuerlyOptions = {}): Promise<Querly> {
    return new Querly({ ...options, cwd: project });
  }

  check(file: string): CheckResult {
    const result = run(this.options.executable ?? "oq", ["check", file, "--json"], this.options.cwd);
    if (result.status !== 0 && result.stdout.trim() === "") {
      throw new Error(result.stderr.trim() || `oq check failed with status ${result.status}`);
    }
    const diagnostics = result.stdout.trim() ? JSON.parse(result.stdout) as Diagnostic[] : [];
    return { ok: result.status === 0, diagnostics };
  }

  compile(file: string, dialect = "sqlite"): CompileResult {
    const result = run(
      this.options.executable ?? "oq",
      ["compile", file, "--dialect", dialect, "--json"],
      this.options.cwd,
    );
    if (result.status !== 0) {
      throw new Error(result.stderr.trim() || `oq compile failed with status ${result.status}`);
    }
    const compiled = JSON.parse(result.stdout) as { dialect: string; sql: string };
    return {
      ok: true,
      artifact: { kind: "sql", dialect: compiled.dialect, text: compiled.sql },
      diagnostics: [],
    };
  }
}
