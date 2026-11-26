import { useState, useEffect, useRef } from "react";
import Editor, { Monaco, OnMount } from "@monaco-editor/react";
import { Sidebar, Toolbar } from "./components/Layout";
import { SchemaExplorerPanel } from "./components/SchemaExplorerPanel";
import { registerQuerlyLanguage } from "./monaco/querly";

const DEFAULT_TEMPLATES: Record<string, string> = {
  english: `-- Querly English Syntax
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

INSERT INTO users (name, email, age)
VALUES ("Ada Lovelace", "ada@example.com", 36)
RETURNING id, name;
`,
  structured: `-- Querly Structured Syntax
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
`,
};

type ResultTab = "diagnostics" | "sql" | "results" | "ir" | "schema" | "explain";

export default function App({
  apiFetch,
}: {
  apiFetch: (path: string, init?: RequestInit) => Promise<any>;
}) {
  const [activeTab, setActiveTab] = useState<"editor" | "converter">("editor");
  const [panelTab, setPanelTab] = useState<ResultTab>("diagnostics");

  const [sourceCode, setSourceCode] = useState(DEFAULT_TEMPLATES.structured);
  const [dialect, setDialect] = useState("sqlite");

  const [result, setResult] = useState<any>(null);
  const [execResult, setExecResult] = useState<any>(null);
  const [explainResult, setExplainResult] = useState<any>(null);
  const [profiles, setProfiles] = useState<any[]>([]);
  const [selectedProfile, setSelectedProfile] = useState<string>("");

  const [loading, setLoading] = useState(false);
  const [profilesLoading, setProfilesLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showConnectionModal, setShowConnectionModal] = useState(false);

  const editorRef = useRef<any>(null);
  const monacoRef = useRef<Monaco | null>(null);

  useEffect(() => {
    loadProfiles();
  }, []);

  async function loadProfiles() {
    setProfilesLoading(true);
    try {
      const res = await apiFetch("/connections/profiles");
      setProfiles(res);
      if (res.length > 0 && !selectedProfile) {
        setSelectedProfile(res[0].id);
      }
    } catch (e) {
      console.warn("Failed to load profiles", e);
    } finally {
      setProfilesLoading(false);
    }
  }

  const handleEditorMount: OnMount = (editor, monaco) => {
    editorRef.current = editor;
    monacoRef.current = monaco;
    registerQuerlyLanguage(monaco);

    // Auto-detect syntax
    updateLanguage(sourceCode);
  };

  function updateLanguage(code: string) {
    if (!monacoRef.current || !editorRef.current) return;

    const model = editorRef.current.getModel();
    if (!model) return;

    // Very simple heuristic: English keywords usually start the file or appear in caps
    const isEnglish = /CREATE\s+TABLE|GET\s+ALL|GET\s+\w+/i.test(code);
    const langId = isEnglish ? "querly-english" : "querly-structured";

    monacoRef.current.editor.setModelLanguage(model, langId);
  }

  const handleEditorChange = (value: string | undefined) => {
    if (value !== undefined) {
      setSourceCode(value);
      // Debounce would be better, but for now:
      if (value.length < 1000) updateLanguage(value);

      // Clear markers on change
      if (monacoRef.current && editorRef.current) {
        const model = editorRef.current.getModel();
        if (model) monacoRef.current.editor.setModelMarkers(model, "querly", []);
      }
    }
  };

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
    } catch (e: any) {
      setError(e.message);
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
      setResult(res);
      setPanelTab(res.ok ? "sql" : "diagnostics");
      updateMarkers(res.diagnostics);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  function updateMarkers(diagnostics: any[]) {
    if (!monacoRef.current || !editorRef.current) return;
    const model = editorRef.current.getModel();
    if (!model) return;

    const markers = diagnostics.map((d) => ({
      startLineNumber: d.line,
      startColumn: d.column,
      endLineNumber: d.endLine,
      endColumn: d.endColumn,
      message: d.message,
      severity: d.severity === 0 ? monacoRef.current!.MarkerSeverity.Error :
                d.severity === 1 ? monacoRef.current!.MarkerSeverity.Warning :
                monacoRef.current!.MarkerSeverity.Info,
    }));

    monacoRef.current.editor.setModelMarkers(model, "querly", markers);
  }

  async function handleExplain() {
    if (!selectedProfile) {
      setError("Please select a database connection.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const comp = await apiFetch("/compiler/compile", {
        method: "POST",
        body: JSON.stringify({ source: sourceCode, dialect }),
      });

      // We explain the first statement if it's a query/select
      const sql = comp.statements?.[0]?.sql || "";
      if (!sql) {
        setError("No executable SQL generated.");
        setLoading(false);
        return;
      }

      const plan = await apiFetch("/execution/explain", {
        method: "POST",
        body: JSON.stringify({
          connectionId: selectedProfile,
          sql: sql,
        }),
      });
      setExplainResult(plan);
      setPanelTab("explain");
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function handleExecute(confirmDestructive = false) {
    if (!selectedProfile) {
      setError("Please select a database connection.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      // 1. Compile
      const comp = await apiFetch("/compiler/compile", {
        method: "POST",
        body: JSON.stringify({ source: sourceCode, dialect }),
      });
      setResult(comp);

      if (!comp.ok) {
        setPanelTab("diagnostics");
        setError("Compilation failed. See diagnostics for details.");
        setLoading(false);
        return;
      }

      // 2. Run
      const exec = await apiFetch("/execution/run", {
        method: "POST",
        body: JSON.stringify({
          connectionId: selectedProfile,
          statements: comp.statements,
          confirmDestructive,
        }),
      });

      if (exec.needsConfirmation) {
        if (confirm("This action is destructive. Are you sure?")) {
           handleExecute(true);
           return;
        }
        setLoading(false);
        return;
      }

      setExecResult(exec);
      setPanelTab("results");
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <img src="/Querly.webp" alt="Querly" className="logo" />
        <h1>QUERLY STUDIO</h1>
        <nav className="tabs">
          <button
            className={`tab ${activeTab === "editor" ? "active" : ""}`}
            onClick={() => setActiveTab("editor")}
          >
            Editor
          </button>
          <button
            className={`tab ${activeTab === "converter" ? "active" : ""}`}
            onClick={() => setActiveTab("converter")}
          >
            Universal Converter
          </button>
        </nav>
      </header>

      <main className="main-container">
        {activeTab === "editor" ? (
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
                onRun={() => handleExecute(false)}
                onReset={() => {
                  setSourceCode(DEFAULT_TEMPLATES.structured);
                  setResult(null);
                  setExecResult(null);
                  setExplainResult(null);
                  setError(null);
                }}
                loading={loading}
              />

              <div className="editor-container">
                <div className="monaco-editor-wrapper">
                  <Editor
                    height="100%"
                    defaultLanguage="querly-structured"
                    theme="querly-dark"
                    value={sourceCode}
                    onChange={handleEditorChange}
                    onMount={handleEditorMount}
                    options={{
                      minimap: { enabled: false },
                      fontSize: 14,
                      lineNumbers: "on",
                      scrollBeyondLastLine: false,
                      automaticLayout: true,
                    }}
                  />
                </div>

                {error && <div className="error-banner">{error}</div>}

                <div className="bottom-panel">
                  <div className="panel-tabs">
                    <div
                      className={`panel-tab ${panelTab === "diagnostics" ? "active" : ""}`}
                      onClick={() => setPanelTab("diagnostics")}
                    >
                      Diagnostics {result?.diagnostics?.length > 0 && `(${result.diagnostics.length})`}
                    </div>
                    <div
                      className={`panel-tab ${panelTab === "sql" ? "active" : ""}`}
                      onClick={() => setPanelTab("sql")}
                    >
                      Compiled SQL
                    </div>
                    <div
                      className={`panel-tab ${panelTab === "results" ? "active" : ""}`}
                      onClick={() => setPanelTab("results")}
                    >
                      Results
                    </div>
                    <div
                      className={`panel-tab ${panelTab === "ir" ? "active" : ""}`}
                      onClick={() => setPanelTab("ir")}
                    >
                      Universal IR
                    </div>
                    <div
                      className={`panel-tab ${panelTab === "schema" ? "active" : ""}`}
                      onClick={() => setPanelTab("schema")}
                    >
                      Schema
                    </div>
                    <div
                      className={`panel-tab ${panelTab === "explain" ? "active" : ""}`}
                      onClick={() => setPanelTab("explain")}
                    >
                      Explain Plan
                    </div>
                  </div>

                  <div className="panel-content">
                    {panelTab === "diagnostics" && result && (
                      <div>
                        <div className="stages">
                          {result.stages.map((s: any, i: number) => (
                            <div key={i} className={`stage stage-${s.status}`}>
                              <strong>{s.name}</strong>: {s.detail} ({s.durationMs}ms)
                            </div>
                          ))}
                        </div>
                        {result.diagnostics.length === 0 ? (
                          <div className="empty-state">No issues found.</div>
                        ) : (
                          result.diagnostics.map((d: any, i: number) => (
                            <div key={i} className={`diag diag-${d.severity}`}>
                              [{d.code}] Line {d.line}, Col {d.column}: {d.message}
                            </div>
                          ))
                        )}
                      </div>
                    )}

                    {panelTab === "sql" && result?.statements && (
                      <div>
                        {result.statements.length === 0 ? (
                          <div className="empty-state">No SQL generated.</div>
                        ) : (
                          result.statements.map((st: any, i: number) => (
                            <pre key={i}>{st.sql}</pre>
                          ))
                        )}
                      </div>
                    )}

                    {panelTab === "results" && execResult && (
                      <div>
                        {execResult.statements.map((st: any, i: number) => (
                          <div key={i} className="exec-stmt">
                            <h4 style={{ margin: "8px 0", fontSize: "12px" }}>
                              Statement {i + 1} ({st.kind}) -{" "}
                              <span className={st.ok ? "ok-badge" : "err-badge"}>
                                {st.ok ? "Success" : "Error"}
                              </span>
                            </h4>
                            {st.error && <p className="err-text">{st.error}</p>}
                            {st.columns && st.columns.length > 0 && (
                              <table className="results-grid">
                                <thead>
                                  <tr>
                                    {st.columns.map((c: any, ci: number) => (
                                      <th key={ci}>{c.name}</th>
                                    ))}
                                  </tr>
                                </thead>
                                <tbody>
                                  {st.rows.map((r: any[], ri: number) => (
                                    <tr key={ri}>
                                      {r.map((cell, ci) => (
                                        <td key={ci}>{String(cell)}</td>
                                      ))}
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            )}
                          </div>
                        ))}
                      </div>
                    )}

                    {panelTab === "ir" && result && (
                      <pre>{JSON.stringify({ schema: result.schemaIr, queries: result.queryIr }, null, 2)}</pre>
                    )}

                    {panelTab === "explain" && explainResult && (
                      <pre>{JSON.stringify(explainResult, null, 2)}</pre>
                    )}

                    {panelTab === "schema" && (
                      selectedProfile ? (
                        <SchemaExplorerPanel
                          apiFetch={apiFetch}
                          profileId={selectedProfile}
                          dialect={dialect}
                        />
                      ) : (
                        <div className="empty-state">Please select a connection to view schema.</div>
                      )
                    )}
                  </div>
                </div>
              </div>
            </div>
          </>
        ) : (
          <div className="workspace">
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

function ConnectionModal({
  apiFetch,
  onClose,
  onCreated,
}: {
  apiFetch: (path: string, init?: RequestInit) => Promise<any>;
  onClose: () => void;
  onCreated: (p: any) => void;
}) {
  const [name, setName] = useState("Local SQLite DB");
  const [path, setPath] = useState("C:\\data\\querly.db");
  const [createIfMissing, setCreateIfMissing] = useState(true);
  const [readOnly, setReadOnly] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const res = await apiFetch("/connections/profiles", {
        method: "POST",
        body: JSON.stringify({
          name,
          adapter: "sqlite",
          path,
          createIfMissing,
          readOnly,
        }),
      });
      onCreated(res);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="modal">
        <h3>New SQLite Connection</h3>
        <form onSubmit={handleSubmit}>
          <div className="form-group">
            <label>Connection Name</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
          </div>
          <div className="form-group">
            <label>Database File Path (Absolute)</label>
            <input
              type="text"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              required
            />
          </div>
          <div className="form-group checkbox">
            <label>
              <input
                type="checkbox"
                checked={createIfMissing}
                onChange={(e) => setCreateIfMissing(e.target.checked)}
              />
              Create file if missing
            </label>
          </div>
          <div className="form-group checkbox">
            <label>
              <input
                type="checkbox"
                checked={readOnly}
                onChange={(e) => setReadOnly(e.target.checked)}
              />
              Read-Only Mode
            </label>
          </div>
          {error && <div className="error-banner">{error}</div>}
          <div className="modal-actions">
            <button type="button" onClick={onClose} className="btn-secondary" disabled={submitting}>
              Cancel
            </button>
            <button type="submit" className="btn-primary" disabled={submitting}>
              {submitting ? "Creating..." : "Create Connection"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function SchemaConverterPanel({
  apiFetch,
}: {
  apiFetch: (path: string, init?: RequestInit) => Promise<any>;
}) {
  const [sourceDialect, setSourceDialect] = useState<string>("sqlite");
  const [targetDialect, setTargetDialect] = useState<string>("postgres");
  const [sourceSchema, setSourceSchema] = useState<string>("");
  const [conversionResult, setConversionResult] = useState<any>(null);
  const [loading, setLoading] = useState<boolean>(false);

  const handleConvert = async () => {
    if (!sourceSchema) return;
    setLoading(true);
    setConversionResult(null);
    try {
      const response = await apiFetch("/converter/convert", {
        method: "POST",
        body: JSON.stringify({
          sourceSchema: sourceSchema,
          sourceDialect,
          targetDialect,
        }),
      });
      setConversionResult(response);
    } catch (error) {
      console.error("Conversion failed:", error);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ padding: "40px", textAlign: "center" }}>
      <h2>Universal Schema Converter</h2>
      <p>Convert schemas between 26+ database dialects instantly.</p>

      <div style={{ marginTop: "20px" }}>
        <label>Source Dialect:
          <select
            value={sourceDialect}
            onChange={(e) => setSourceDialect(e.target.value)}
            style={{ marginRight: "8px" }}
          >
            <option value="sqlite">SQLite</option>
            <option value="postgres">PostgreSQL</option>
            <option value="mysql">MySQL</option>
          </select>
        </label>
        <label>Target Dialect:
          <select
            value={targetDialect}
            onChange={(e) => setTargetDialect(e.target.value)}
            style={{ marginRight: "8px" }}
          >
            <option value="postgres">PostgreSQL</option>
            <option value="mysql">MySQL</option>
            <option value="sqlite">SQLite</option>
          </select>
        </label>
      </div>

      <div style={{ marginTop: "20px" }}>
        <textarea
          rows={8}
          cols={50}
          placeholder="Enter source schema as JSON IR..."
          value={sourceSchema}
          onChange={(e) => setSourceSchema(e.target.value)}
          style={{ width: "100%", marginBottom: "12px", background: "#1e1e1e", color: "#e0e0e0", border: "1px solid #333", padding: "8px", borderRadius: "4px" }}
        ></textarea>
        <div style={{ marginBottom: "12px", fontSize: "12px", color: "#858585" }}>
          Tip: Use the Editor tab to compile a schema first, then copy the Universal IR from the "Universal IR" tab.
        </div>
        <button
          onClick={handleConvert}
          disabled={loading}
          style={{
            background: "#bb86fc",
            color: "#000",
            border: "none",
            padding: "8px 16px",
            borderRadius: "4px",
            cursor: loading ? "not-allowed" : "pointer",
          }}
        >
          {loading ? "Converting..." : "Convert Schema"}
        </button>
      </div>

      {conversionResult && (
        <div style={{ marginTop: "20px", textAlign: "left", background: "#1e1e1e", padding: "12px", borderRadius: "4px" }}>
          <h4>Conversion Result</h4>
          <p><strong>Status:</strong> {conversionResult.conversion_status}</p>
          <p><strong>Profile:</strong> {conversionResult.profile}</p>
          <div style={{ marginTop: "12px" }}>
            <strong>Findings:</strong>
            {conversionResult.findings && conversionResult.findings.length > 0 ? (
              <ul>
                {conversionResult.findings.map((f: Record<string, any>, i: number) => (
                  <li key={i}>
                    <strong>{String(f['source']?.['name'] || f.construct || 'attr')}:</strong> {String(f.status)} {f.notes ? `(${String(f.notes)})` : ''}
                  </li>
                ))}
              </ul>
            ) : (
              <p>No findings</p>
            )}
          </div>
          <div style={{ marginTop: "12px" }}>
            <strong>Migration Plan:</strong>
            {conversionResult.migration_plan && conversionResult.migration_plan.length > 0 ? (
              <ul>
                {conversionResult.migration_plan.map((artifact: Record<string, any>, i: number) => (
                  <li key={i}>
                    <strong>{String(artifact.entity || 'entity')}:</strong> {String(artifact.statement_type)} {artifact.sql ? `(SQL: ${String(artifact.sql)})` : ''}
                  </li>
                ))}
              </ul>
            ) : (
              <p>No migration artifacts</p>
            )}
          </div>
        </div>
      )}

      <div style={{ marginTop: "20px", color: "#858585", fontSize: "12px" }}>
        Phase 15: Schema conversion between dialects
      </div>
    </div>
  );
}
