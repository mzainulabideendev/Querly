import { useEffect, useRef, useState } from "react";
import Editor, { Monaco, OnMount } from "@monaco-editor/react";
import { DIALECTS, Sidebar, Toolbar, dialectLabel } from "./components/Layout";
import { SchemaExplorerPanel } from "./components/SchemaExplorerPanel";
import { registerQuerlyLanguage } from "./monaco/querly";

type ApiFetch = (path: string, init?: RequestInit) => Promise<any>;
type View = "ide" | "translate" | "schema";
type PanelTab = "diagnostics" | "sql" | "results" | "ir" | "schema" | "explain";

const DEFAULT_TEMPLATE = `-- Querly language (structured syntax)
-- Write once, compile to any database below.
table users {
  id: integer @primary @auto
  name: text @required
  email: email @unique
  age: integer?
  created_at: timestamp @default(now())
}

query adults {
  from users
  select id, name, age
  where age > 18
  order by name asc
  limit 50
}

insert into users (name, email, age)
values ("Ada Lovelace", "ada@example.com", 36)
returning id, name
`;

const ENGLISH_TEMPLATE = `-- Querly language (English syntax)
CREATE TABLE users WITH:
  id AS REQUIRED NUMBER PRIMARY KEY AUTO
  email AS REQUIRED TEXT UNIQUE
  name AS REQUIRED TEXT
  age AS OPTIONAL NUMBER
  created_at AS REQUIRED TIMESTAMP DEFAULT(NOW())

GET id, name, email FROM users
WHERE age IS GREATER THAN 18
SORT BY name ASCENDING
LIMIT 50;
`;

function severityClass(severity: string): string {
  if (severity === "error") return "error";
  if (severity === "warning") return "warning";
  if (severity === "hint") return "hint";
  return "info";
}

function markerSeverity(monaco: Monaco, severity: string) {
  if (severity === "error") return monaco.MarkerSeverity.Error;
  if (severity === "warning") return monaco.MarkerSeverity.Warning;
  if (severity === "hint") return monaco.MarkerSeverity.Hint;
  return monaco.MarkerSeverity.Info;
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function downloadText(filename: string, text: string) {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export default function App({ apiFetch }: { apiFetch: ApiFetch }) {
  const [view, setView] = useState<View>("ide");
  const [panelTab, setPanelTab] = useState<PanelTab>("sql");

  const [sourceCode, setSourceCode] = useState(DEFAULT_TEMPLATE);
  const [dialect, setDialect] = useState("postgres");

  const [result, setResult] = useState<any>(null);
  const [execResult, setExecResult] = useState<any>(null);
  const [explainResult, setExplainResult] = useState<any>(null);
  const [profiles, setProfiles] = useState<any[]>([]);
  const [selectedProfile, setSelectedProfile] = useState<string>("");

  const [loading, setLoading] = useState(false);
  const [profilesLoading, setProfilesLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showConnectionModal, setShowConnectionModal] = useState(false);

  const editorRef = useRef<any>(null);
  const monacoRef = useRef<Monaco | null>(null);

  useEffect(() => {
    void loadProfiles();
  }, []);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(null), 2400);
    return () => window.clearTimeout(timer);
  }, [notice]);

  async function loadProfiles() {
    setProfilesLoading(true);
    try {
      const res = await apiFetch("/connections/profiles");
      setProfiles(res);
      if (res.length > 0 && !selectedProfile) {
        setSelectedProfile(res[0].id);
      }
    } catch (err) {
      console.warn("Failed to load profiles", err);
    } finally {
      setProfilesLoading(false);
    }
  }

  const beforeMountQuerly = (monaco: Monaco) => {
    registerQuerlyLanguage(monaco);
  };

  const handleEditorMount: OnMount = (editor, monaco) => {
    editorRef.current = editor;
    monacoRef.current = monaco;
  };

  const handleEditorChange = (value: string | undefined) => {
    if (value === undefined) return;
    setSourceCode(value);
    if (monacoRef.current && editorRef.current) {
      const model = editorRef.current.getModel();
      if (model) {
        monacoRef.current.editor.setModelMarkers(model, "querly", []);
      }
    }
  };

  function updateMarkers(diagnostics: any[]) {
    if (!monacoRef.current || !editorRef.current) return;
    const model = editorRef.current.getModel();
    if (!model) return;

    const markers = (diagnostics ?? []).map((diagnostic) => ({
      startLineNumber: diagnostic.line ?? 1,
      startColumn: diagnostic.column ?? 1,
      endLineNumber: diagnostic.endLine ?? diagnostic.line ?? 1,
      endColumn: diagnostic.endColumn ?? diagnostic.column ?? 1,
      message: diagnostic.message ?? "Unknown diagnostic",
      severity: markerSeverity(monacoRef.current!, diagnostic.severity),
    }));

    monacoRef.current.editor.setModelMarkers(model, "querly", markers);
  }

  function applyCompilation(res: any) {
    setResult(res);
    setPanelTab(res.ok ? "sql" : "diagnostics");
    updateMarkers(res.diagnostics);
  }

  async function handleCheck() {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/compiler/check", {
        method: "POST",
        body: JSON.stringify({ source: sourceCode }),
      });
      setResult(res);
      setPanelTab("diagnostics");
      updateMarkers(res.diagnostics);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleCompile() {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/compiler/compile", {
        method: "POST",
        body: JSON.stringify({ source: sourceCode, dialect }),
      });
      applyCompilation(res);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleExplain() {
    if (!selectedProfile) {
      setError("Select a database connection before requesting an execution plan.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const compilation = await apiFetch("/compiler/compile", {
        method: "POST",
        body: JSON.stringify({ source: sourceCode, dialect }),
      });
      applyCompilation(compilation);

      const sql = compilation.statements?.[0]?.sql || "";
      if (!sql) {
        setError("No executable SQL was generated.");
        return;
      }

      const plan = await apiFetch("/execution/explain", {
        method: "POST",
        body: JSON.stringify({ connectionId: selectedProfile, sql }),
      });
      setExplainResult(plan);
      setPanelTab("explain");
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleExecute(confirmDestructive = false) {
    if (!selectedProfile) {
      setError("Select a database connection before running a program.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const compilation = await apiFetch("/compiler/compile", {
        method: "POST",
        body: JSON.stringify({ source: sourceCode, dialect }),
      });
      applyCompilation(compilation);

      if (!compilation.ok) {
        setError("Compilation failed. See diagnostics for details.");
        return;
      }

      const execution = await apiFetch("/execution/run", {
        method: "POST",
        body: JSON.stringify({
          connectionId: selectedProfile,
          source: sourceCode,
          dialect,
          confirmDestructive,
        }),
      });

      if (execution.needsConfirmation) {
        if (confirm("This program contains destructive statements. Run it anyway?")) {
          await handleExecute(true);
        }
        return;
      }

      setExecResult(execution);
      setPanelTab("results");
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  const generatedSql = (result?.statements ?? [])
    .map((statement: any) => statement.sql)
    .join("\n\n");

  const diagnosticCount = result?.diagnostics?.length ?? 0;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <img src="/Querly.webp" alt="Querly" className="logo" />
          <div className="brand-text">
            <span className="brand-name">Querly Studio</span>
            <span className="brand-tag">Universal Database IDE</span>
          </div>
        </div>

        <nav className="tabs">
          <button
            className={`tab ${view === "ide" ? "active" : ""}`}
            onClick={() => setView("ide")}
          >
            SQL IDE
          </button>
          <button
            className={`tab ${view === "translate" ? "active" : ""}`}
            onClick={() => setView("translate")}
          >
            Querly → SQL
          </button>
          <button
            className={`tab ${view === "schema" ? "active" : ""}`}
            onClick={() => setView("schema")}
          >
            Schema Converter
          </button>
        </nav>

        <div className="topbar-meta">
          {result?.compilerVersion && <span>compiler {result.compilerVersion}</span>}
        </div>
      </header>

      {notice && <div className="notice-banner">{notice}</div>}

      <main className="main-container">
        {view === "ide" && (
          <>
            <Sidebar
              profiles={profiles}
              selectedProfile={selectedProfile}
              onSelectProfile={setSelectedProfile}
              onNewProfile={() => setShowConnectionModal(true)}
              loading={profilesLoading}
            />
            <div className="workspace">
              <Toolbar
                dialect={dialect}
                onDialectChange={setDialect}
                onCheck={handleCheck}
                onCompile={handleCompile}
                onExplain={handleExplain}
                onRun={() => void handleExecute(false)}
                onReset={() => {
                  setSourceCode(DEFAULT_TEMPLATE);
                  setResult(null);
                  setExecResult(null);
                  setExplainResult(null);
                  setError(null);
                  if (monacoRef.current && editorRef.current) {
                    const model = editorRef.current.getModel();
                    if (model) monacoRef.current.editor.setModelMarkers(model, "querly", []);
                  }
                }}
                loading={loading}
              />

              <div className="editor-container">
                <div className="monaco-editor-wrapper">
                  <Editor
                    height="100%"
                    language="querly"
                    theme="querly-dark"
                    value={sourceCode}
                    beforeMount={beforeMountQuerly}
                    onChange={handleEditorChange}
                    onMount={handleEditorMount}
                    options={{
                      minimap: { enabled: false },
                      fontSize: 14,
                      lineNumbers: "on",
                      scrollBeyondLastLine: false,
                      automaticLayout: true,
                      tabSize: 2,
                      wordWrap: "off",
                      smoothScrolling: true,
                      renderWhitespace: "selection",
                      fontLigatures: true,
                      padding: { top: 12, bottom: 12 },
                    }}
                  />
                </div>

                <div className="statusbar">
                  <span className="status-pill">querly</span>
                  <span>
                    target <strong>{dialectLabel(dialect)}</strong>
                  </span>
                  {result && (
                    <span className={result.ok ? "status-ok" : "status-error"}>
                      {result.ok ? "compiles cleanly" : `${diagnosticCount} issue(s)`}
                    </span>
                  )}
                  <span className="statusbar-spacer" />
                  {result?.stats && (
                    <span>
                      {result.stats.declarations} declaration(s) ·{" "}
                      {result.stats.statements} statement(s) ·{" "}
                      {Math.round(result.stats.durationMs ?? 0)} ms
                    </span>
                  )}
                </div>

                {error && <div className="error-banner">{error}</div>}

                <div className="bottom-panel">
                  <div className="panel-tabs">
                    <button
                      className={`panel-tab ${panelTab === "diagnostics" ? "active" : ""}`}
                      onClick={() => setPanelTab("diagnostics")}
                    >
                      Diagnostics {diagnosticCount > 0 && `(${diagnosticCount})`}
                    </button>
                    <button
                      className={`panel-tab ${panelTab === "sql" ? "active" : ""}`}
                      onClick={() => setPanelTab("sql")}
                    >
                      Compiled SQL {generatedSql ? `(${result.statements.length})` : ""}
                    </button>
                    <button
                      className={`panel-tab ${panelTab === "results" ? "active" : ""}`}
                      onClick={() => setPanelTab("results")}
                    >
                      Results
                    </button>
                    <button
                      className={`panel-tab ${panelTab === "ir" ? "active" : ""}`}
                      onClick={() => setPanelTab("ir")}
                    >
                      Universal IR
                    </button>
                    <button
                      className={`panel-tab ${panelTab === "schema" ? "active" : ""}`}
                      onClick={() => setPanelTab("schema")}
                    >
                      Schema
                    </button>
                    <button
                      className={`panel-tab ${panelTab === "explain" ? "active" : ""}`}
                      onClick={() => setPanelTab("explain")}
                    >
                      Explain Plan
                    </button>
                  </div>

                  <div className="panel-content">
                    {panelTab === "diagnostics" && (
                      <div>
                        {!result ? (
                          <div className="empty-state">
                            Run <strong>Check</strong> or <strong>Compile</strong> to validate your
                            Querly program.
                          </div>
                        ) : (
                          <>
                            <div className="stages">
                              {result.stages?.map((stage: any, index: number) => (
                                <div
                                  key={index}
                                  className={`stage stage-${stage.status}`}
                                  title={stage.detail}
                                >
                                  <strong>{stage.name}</strong>
                                  <span>{stage.status}</span>
                                </div>
                              ))}
                            </div>
                            {diagnosticCount === 0 ? (
                              <div className="empty-state success">
                                No issues found — this program is valid Querly.
                              </div>
                            ) : (
                              result.diagnostics.map((diagnostic: any, index: number) => (
                                <div
                                  key={index}
                                  className={`diag diag-${severityClass(diagnostic.severity)}`}
                                >
                                  <span className="diag-code">{diagnostic.code}</span>
                                  <span>
                                    Line {diagnostic.line}, Col {diagnostic.column}:{" "}
                                    {diagnostic.message}
                                  </span>
                                </div>
                              ))
                            )}
                          </>
                        )}
                      </div>
                    )}

                    {panelTab === "sql" && (
                      <div>
                        {!result?.statements || result.statements.length === 0 ? (
                          <div className="empty-state">
                            Compile a Querly program to see SQL for{" "}
                            <strong>{dialectLabel(dialect)}</strong>.
                          </div>
                        ) : (
                          <>
                            <div className="panel-actions">
                              <span className="muted">
                                {result.statements.length} statement(s) for{" "}
                                {dialectLabel(result.dialect)}
                              </span>
                              <div className="actions">
                                <button
                                  className="btn-secondary small"
                                  onClick={async () => {
                                    if (await copyText(generatedSql)) setNotice("SQL copied");
                                    else setError("Clipboard is not available.");
                                  }}
                                >
                                  Copy SQL
                                </button>
                                <button
                                  className="btn-secondary small"
                                  onClick={() =>
                                    downloadText(`querly-${result.dialect}.sql`, generatedSql)
                                  }
                                >
                                  Download .sql
                                </button>
                              </div>
                            </div>
                            {result.statements.map((statement: any, index: number) => (
                              <div key={index} className="sql-block">
                                <div className="sql-block-header">
                                  <span className="badge">{statement.kind}</span>
                                  <span className="muted">{statement.id}</span>
                                </div>
                                <pre>{statement.sql}</pre>
                              </div>
                            ))}
                          </>
                        )}
                      </div>
                    )}

                    {panelTab === "results" && (
                      <div>
                        {!execResult ? (
                          <div className="empty-state">
                            Select a connection and press <strong>Run</strong> to execute the
                            compiled program.
                          </div>
                        ) : (
                          execResult.statements?.map((statement: any, index: number) => (
                            <div key={index} className="exec-stmt">
                              <h4 className="exec-title">
                                Statement {index + 1} ({statement.kind}){" "}
                                <span className={statement.ok ? "ok-badge" : "err-badge"}>
                                  {statement.ok ? "Success" : "Error"}
                                </span>
                              </h4>
                              {statement.error && <p className="err-text">{statement.error}</p>}
                              {statement.columns && statement.columns.length > 0 && (
                                <div className="table-scroll">
                                  <table className="results-grid">
                                    <thead>
                                      <tr>
                                        {statement.columns.map((column: any, ci: number) => (
                                          <th key={ci}>{column.name}</th>
                                        ))}
                                      </tr>
                                    </thead>
                                    <tbody>
                                      {statement.rows.map((row: any[], ri: number) => (
                                        <tr key={ri}>
                                          {row.map((cell, ci) => (
                                            <td key={ci}>
                                              {cell === null ? (
                                                <span className="null-cell">NULL</span>
                                              ) : (
                                                String(cell)
                                              )}
                                            </td>
                                          ))}
                                        </tr>
                                      ))}
                                    </tbody>
                                  </table>
                                </div>
                              )}
                            </div>
                          ))
                        )}
                      </div>
                    )}

                    {panelTab === "ir" && (
                      <div>
                        {!result ? (
                          <div className="empty-state">Compile to generate the universal IR.</div>
                        ) : (
                          <pre>
                            {JSON.stringify(
                              { schema: result.schemaIr, queries: result.queryIr },
                              null,
                              2,
                            )}
                          </pre>
                        )}
                      </div>
                    )}

                    {panelTab === "explain" && (
                      <div>
                        {!explainResult ? (
                          <div className="empty-state">
                            Compile a query and press <strong>Explain</strong> to inspect the
                            engine plan.
                          </div>
                        ) : (
                          <pre>{JSON.stringify(explainResult, null, 2)}</pre>
                        )}
                      </div>
                    )}

                    {panelTab === "schema" && (
                      <div>
                        {selectedProfile ? (
                          <SchemaExplorerPanel
                            apiFetch={apiFetch}
                            profileId={selectedProfile}
                            dialect={dialect}
                          />
                        ) : (
                          <div className="empty-state">
                            Add and select a connection to inspect its schema.
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </>
        )}

        {view === "translate" && (
          <TranslateView
            apiFetch={apiFetch}
            onOpenInIde={(source, targetDialect) => {
              setSourceCode(source);
              setDialect(targetDialect);
              setResult(null);
              setError(null);
              setView("ide");
            }}
          />
        )}

        {view === "schema" && (
          <div className="workspace schema-view">
            <SchemaConverterPanel apiFetch={apiFetch} />
          </div>
        )}
      </main>

      {showConnectionModal && (
        <ConnectionModal
          apiFetch={apiFetch}
          onClose={() => setShowConnectionModal(false)}
          onCreated={(newProfile) => {
            setProfiles((prev) => [...prev, newProfile]);
            setSelectedProfile(newProfile.id);
            setShowConnectionModal(false);
          }}
        />
      )}
    </div>
  );
}

function TranslateView({
  apiFetch,
  onOpenInIde,
}: {
  apiFetch: ApiFetch;
  onOpenInIde: (source: string, targetDialect: string) => void;
}) {
  const [source, setSource] = useState(DEFAULT_TEMPLATE);
  const [target, setTarget] = useState("postgres");
  const [result, setResult] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const monacoRef = useRef<Monaco | null>(null);
  const editorRef = useRef<any>(null);

  async function translate() {
    setLoading(true);
    setError(null);
    try {
      const response = await apiFetch("/compiler/compile", {
        method: "POST",
        body: JSON.stringify({ source, dialect: target }),
      });
      setResult(response);

      const model = editorRef.current?.getModel();
      if (model && monacoRef.current) {
        monacoRef.current.editor.setModelMarkers(
          model,
          "querly",
          (response.diagnostics ?? []).map((diagnostic: any) => ({
            startLineNumber: diagnostic.line ?? 1,
            startColumn: diagnostic.column ?? 1,
            endLineNumber: diagnostic.endLine ?? diagnostic.line ?? 1,
            endColumn: diagnostic.endColumn ?? diagnostic.column ?? 1,
            message: diagnostic.message,
            severity: markerSeverity(monacoRef.current!, diagnostic.severity),
          })),
        );
      }
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  const sql = (result?.statements ?? []).map((statement: any) => statement.sql).join("\n\n");

  return (
    <div className="translate-layout">
      <section className="panel-card">
        <header className="card-header">
          <div>
            <h2>Querly source</h2>
            <p className="muted">
              Write the Querly language (structured or English syntax), then translate it.
            </p>
          </div>
          <div className="actions">
            <button className="btn-ghost small" onClick={() => setSource(DEFAULT_TEMPLATE)}>
              Structured sample
            </button>
            <button className="btn-ghost small" onClick={() => setSource(ENGLISH_TEMPLATE)}>
              English sample
            </button>
          </div>
        </header>
        <div className="translate-editor">
          <Editor
            height="100%"
            language="querly"
            theme="querly-dark"
            value={source}
            beforeMount={(monaco: Monaco) => {
              registerQuerlyLanguage(monaco);
            }}
            onChange={(value) => {
              setSource(value ?? "");
              setResult(null);
            }}
            onMount={(editor, monaco) => {
              editorRef.current = editor;
              monacoRef.current = monaco;
            }}
            options={{
              minimap: { enabled: false },
              fontSize: 14,
              automaticLayout: true,
              scrollBeyondLastLine: false,
              tabSize: 2,
              padding: { top: 12, bottom: 12 },
            }}
          />
        </div>
      </section>

      <section className="panel-card">
        <header className="card-header">
          <div>
            <h2>Target database</h2>
            <p className="muted">Querly IR → native SQL for the selected engine.</p>
          </div>
        </header>

        <div className="dialect-chips">
          {DIALECTS.map((item) => (
            <button
              key={item.id}
              className={`dialect-chip ${target === item.id ? "active" : ""}`}
              onClick={() => {
                setTarget(item.id);
                setResult(null);
              }}
            >
              <span className="dialect-short">{item.short}</span>
              {item.label}
            </button>
          ))}
        </div>

        <button className="btn-primary translate-btn" onClick={() => void translate()} disabled={loading}>
          {loading ? "Translating…" : `Translate to ${dialectLabel(target)}`}
        </button>

        {error && <div className="error-banner">{error}</div>}

        <div className="translate-output">
          {notice && <div className="notice-banner inline">{notice}</div>}
          {!result ? (
            <div className="empty-state">
              The generated SQL will appear here. You can copy it or open it in the SQL IDE.
            </div>
          ) : !result.ok ? (
            <div>
              <div className="error-banner">
                Querly could not be compiled. Fix {result.diagnostics.length} diagnostic(s).
              </div>
              {result.diagnostics.map((diagnostic: any, index: number) => (
                <div
                  key={index}
                  className={`diag diag-${severityClass(diagnostic.severity)}`}
                >
                  <span className="diag-code">{diagnostic.code}</span>
                  Line {diagnostic.line}: {diagnostic.message}
                </div>
              ))}
            </div>
          ) : (
            <div>
              <div className="panel-actions">
                <span className="badge">{dialectLabel(result.dialect)}</span>
                <div className="actions">
                  <button
                    className="btn-secondary small"
                    onClick={async () => {
                      if (await copyText(sql)) {
                        setNotice("SQL copied");
                        window.setTimeout(() => setNotice(null), 1800);
                      }
                    }}
                  >
                    Copy SQL
                  </button>
                  <button
                    className="btn-secondary small"
                    onClick={() => downloadText(`querly-${result.dialect}.sql`, sql)}
                  >
                    Download
                  </button>
                  <button
                    className="btn-secondary small"
                    onClick={() => onOpenInIde(source, result.dialect)}
                  >
                    Open in IDE
                  </button>
                </div>
              </div>
              {result.statements.map((statement: any, index: number) => (
                <div key={index} className="sql-block">
                  <div className="sql-block-header">
                    <span className="badge">{statement.kind}</span>
                    <span className="muted">{statement.id}</span>
                  </div>
                  <pre>{statement.sql}</pre>
                </div>
              ))}
              {result.warnings?.length > 0 && (
                <div className="warnings">
                  {result.warnings.map((warning: string, index: number) => (
                    <div key={index} className="diag diag-warning">
                      {warning}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

function ConnectionModal({
  apiFetch,
  onClose,
  onCreated,
}: {
  apiFetch: ApiFetch;
  onClose: () => void;
  onCreated: (profile: any) => void;
}) {
  const isWindows = navigator.userAgent.includes("Windows");
  const [name, setName] = useState("Local SQLite DB");
  const [path, setPath] = useState(isWindows ? "C:\\data\\querly.db" : "/tmp/querly.db");
  const [createIfMissing, setCreateIfMissing] = useState(true);
  const [readOnly, setReadOnly] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const response = await apiFetch("/connections/profiles", {
        method: "POST",
        body: JSON.stringify({
          name,
          adapter: "sqlite",
          path,
          createIfMissing,
          readOnly,
        }),
      });
      onCreated(response);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="modal">
        <h3>New SQLite connection</h3>
        <p className="muted">Querly Studio stores connection profiles locally.</p>
        <form onSubmit={(event) => void handleSubmit(event)}>
          <div className="form-group">
            <label>Connection name</label>
            <input
              type="text"
              value={name}
              onChange={(event) => setName(event.target.value)}
              required
            />
          </div>
          <div className="form-group">
            <label>Database file path (absolute)</label>
            <input
              type="text"
              value={path}
              onChange={(event) => setPath(event.target.value)}
              required
            />
          </div>
          <div className="form-group checkbox">
            <label>
              <input
                type="checkbox"
                checked={createIfMissing}
                onChange={(event) => setCreateIfMissing(event.target.checked)}
              />
              Create the file if it is missing
            </label>
          </div>
          <div className="form-group checkbox">
            <label>
              <input
                type="checkbox"
                checked={readOnly}
                onChange={(event) => setReadOnly(event.target.checked)}
              />
              Read-only mode
            </label>
          </div>
          {error && <div className="error-banner">{error}</div>}
          <div className="modal-actions">
            <button type="button" onClick={onClose} className="btn-secondary" disabled={submitting}>
              Cancel
            </button>
            <button type="submit" className="btn-primary" disabled={submitting}>
              {submitting ? "Creating…" : "Create connection"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function SchemaConverterPanel({ apiFetch }: { apiFetch: ApiFetch }) {
  const [sourceDialect, setSourceDialect] = useState<string>("sqlite");
  const [targetDialect, setTargetDialect] = useState<string>("postgres");
  const [sourceSchema, setSourceSchema] = useState<string>("");
  const [conversionResult, setConversionResult] = useState<any>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const handleConvert = async () => {
    if (!sourceSchema) return;
    setLoading(true);
    setError(null);
    setConversionResult(null);
    try {
      const response = await apiFetch("/converter/convert", {
        method: "POST",
        body: JSON.stringify({ sourceSchema, sourceDialect, targetDialect }),
      });
      setConversionResult(response);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="schema-converter">
      <header className="card-header">
        <div>
          <h2>Universal schema converter</h2>
          <p className="muted">
            Convert a Querly universal schema IR document into migration SQL for another engine.
          </p>
        </div>
      </header>

      <div className="schema-form">
        <label>
          Source dialect
          <select
            value={sourceDialect}
            onChange={(event) => setSourceDialect(event.target.value)}
            className="select"
          >
            {DIALECTS.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Target dialect
          <select
            value={targetDialect}
            onChange={(event) => setTargetDialect(event.target.value)}
            className="select"
          >
            {DIALECTS.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <textarea
        className="schema-input"
        rows={12}
        placeholder="Paste the universal schema IR JSON produced by the SQL IDE…"
        value={sourceSchema}
        onChange={(event) => setSourceSchema(event.target.value)}
      />
      <p className="muted">
        Tip: compile a Querly program in the SQL IDE, open the{" "}
        <strong>Universal IR</strong> tab and copy the <code>schema</code> object here.
      </p>

      <button
        className="btn-primary"
        onClick={() => void handleConvert()}
        disabled={loading || !sourceSchema}
      >
        {loading ? "Converting…" : "Convert schema"}
      </button>

      {error && <div className="error-banner">{error}</div>}

      {conversionResult && (
        <div className="conversion-result">
          <h3>Conversion result</h3>
          <p>
            <strong>Status:</strong> {conversionResult.conversion_status} ·{" "}
            <strong>Profile:</strong> {conversionResult.profile}
          </p>
          <h4>Findings</h4>
          {conversionResult.findings?.length > 0 ? (
            <ul>
              {conversionResult.findings.map((finding: Record<string, any>, index: number) => (
                <li key={index}>
                  <strong>{String(finding.source?.name || finding.construct || "attr")}:</strong>{" "}
                  {String(finding.status)} {finding.notes ? `(${String(finding.notes)})` : ""}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">No findings</p>
          )}
          <h4>Migration plan</h4>
          {conversionResult.migration_plan?.length > 0 ? (
            <ul>
              {conversionResult.migration_plan.map(
                (artifact: Record<string, any>, index: number) => (
                  <li key={index}>
                    <strong>{String(artifact.entity || "entity")}:</strong>{" "}
                    {String(artifact.statement_type)}{" "}
                    {artifact.sql ? `(${String(artifact.sql)})` : ""}
                  </li>
                ),
              )}
            </ul>
          ) : (
            <p className="muted">No migration artifacts</p>
          )}
        </div>
      )}
    </div>
  );
}
