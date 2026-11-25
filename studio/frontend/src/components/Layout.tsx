import React from "react";

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
        <h3>Connections</h3>
        <button className="icon-btn" onClick={onNewProfile} title="Add Connection">
          +
        </button>
      </div>
      <div className="sidebar-content">
        {loading && <div className="loading">Loading...</div>}
        {profiles.length === 0 && !loading && (
          <div className="empty-state">No profiles found</div>
        )}
        <ul className="profile-list">
          {profiles.map((p) => (
            <li
              key={p.id}
              className={`profile-item ${selectedProfile === p.id ? "active" : ""}`}
              onClick={() => onSelectProfile(p.id)}
            >
              <span className={`adapter-icon ${p.adapter}`}>{p.adapter[0].toUpperCase()}</span>
              <span className="profile-name">{p.name}</span>
              {p.hasSecret && <span className="secret-icon">🔒</span>}
            </li>
          ))}
        </ul>
      </div>
    </aside>
  );
}

export function Toolbar({
  dialect,
  onDialectChange,
  onCheck,
  onCompile,
  onRun,
  onReset,
  loading,
}: {
  dialect: string;
  onDialectChange: (d: string) => void;
  onCheck: () => void;
  onCompile: () => void;
  onRun: () => void;
  onReset: () => void;
  loading: boolean;
}) {
  return (
    <div className="toolbar">
      <div className="toolbar-group">
        <label>Dialect:</label>
        <select
          value={dialect}
          onChange={(e) => onDialectChange(e.target.value)}
          className="select"
        >
          <option value="sqlite">SQLite</option>
          <option value="postgres">PostgreSQL</option>
          <option value="mysql">MySQL</option>
          <option value="mariadb">MariaDB</option>
        </select>
      </div>
      <div className="toolbar-divider" />
      <div className="toolbar-group">
        <button onClick={onCheck} disabled={loading} className="btn-secondary">
          Check
        </button>
        <button onClick={onCompile} disabled={loading} className="btn-secondary">
          Compile
        </button>
        <button onClick={onRun} disabled={loading} className="btn-primary">
          Run
        </button>
      </div>
      <div className="toolbar-spacer" />
      <button onClick={onReset} className="btn-ghost">
        Reset
      </button>
    </div>
  );
}
