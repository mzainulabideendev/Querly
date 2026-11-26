export interface Dialect {
  id: string;
  label: string;
  short: string;
}

export const DIALECTS: Dialect[] = [
  { id: "sqlite", label: "SQLite", short: "SQ" },
  { id: "postgres", label: "PostgreSQL", short: "PG" },
  { id: "mysql", label: "MySQL", short: "MY" },
  { id: "sqlserver", label: "SQL Server", short: "MS" },
  { id: "oracle", label: "Oracle", short: "OR" },
  { id: "clickhouse", label: "ClickHouse", short: "CH" },
];

export function dialectLabel(id: string): string {
  return DIALECTS.find((dialect) => dialect.id === id)?.label ?? id;
}

interface SidebarProps {
  profiles: any[];
  selectedProfile: string;
  onSelectProfile: (id: string) => void;
  onNewProfile: () => void;
  loading: boolean;
}

export function Sidebar({
  profiles,
  selectedProfile,
  onSelectProfile,
  onNewProfile,
  loading,
}: SidebarProps) {
  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <div>
          <h3>Connections</h3>
          <p className="sidebar-subtitle">{profiles.length} saved profile(s)</p>
        </div>
        <button className="icon-btn" onClick={onNewProfile} title="Add connection">
          +
        </button>
      </div>
      <div className="sidebar-content">
        {loading && <div className="loading">Loading connections…</div>}
        {profiles.length === 0 && !loading && (
          <div className="empty-state">
            <p>No database connections yet.</p>
            <button className="btn-secondary small" onClick={onNewProfile}>
              Add SQLite connection
            </button>
          </div>
        )}
        <ul className="profile-list">
          {profiles.map((profile) => (
            <li
              key={profile.id}
              className={`profile-item ${selectedProfile === profile.id ? "active" : ""}`}
              onClick={() => onSelectProfile(profile.id)}
            >
              <span className={`adapter-icon ${profile.adapter}`}>
                {(profile.adapter ?? "db").slice(0, 2).toUpperCase()}
              </span>
              <span className="profile-name" title={profile.name}>
                {profile.name}
              </span>
              {profile.hasSecret && <span className="secret-icon">🔒</span>}
            </li>
          ))}
        </ul>
      </div>
      <div className="sidebar-footer">
        <span className="dot online" /> Backend connected
      </div>
    </aside>
  );
}

interface ToolbarProps {
  dialect: string;
  onDialectChange: (dialect: string) => void;
  onCheck: () => void;
  onCompile: () => void;
  onExplain: () => void;
  onRun: () => void;
  onReset: () => void;
  loading: boolean;
}

export function Toolbar({
  dialect,
  onDialectChange,
  onCheck,
  onCompile,
  onExplain,
  onRun,
  onReset,
  loading,
}: ToolbarProps) {
  return (
    <div className="toolbar">
      <div className="toolbar-group">
        <span className="toolbar-label">Target dialect</span>
        <select
          value={dialect}
          onChange={(event) => onDialectChange(event.target.value)}
          className="select"
        >
          {DIALECTS.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>
      </div>
      <div className="toolbar-divider" />
      <div className="toolbar-group">
        <button onClick={onCheck} disabled={loading} className="btn-secondary">
          Check
        </button>
        <button onClick={onCompile} disabled={loading} className="btn-secondary">
          Compile → SQL
        </button>
        <button onClick={onExplain} disabled={loading} className="btn-secondary">
          Explain
        </button>
        <button onClick={onRun} disabled={loading} className="btn-primary">
          {loading ? "Working…" : "Run"}
        </button>
      </div>
      <div className="toolbar-spacer" />
      <button onClick={onReset} className="btn-ghost">
        Reset editor
      </button>
    </div>
  );
}
