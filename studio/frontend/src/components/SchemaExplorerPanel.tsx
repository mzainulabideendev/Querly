import { useState, useEffect } from "react";

interface TableInfo {
  columns?: Record<string, unknown>[];
  indexes?: Record<string, unknown>[];
  foreign_keys?: Record<string, unknown>[];
}

interface SchemaResponse {
  tables?: Record<string, TableInfo>;
}

interface SchemaExplorerProps {
  apiFetch: (path: string, init?: RequestInit) => Promise<SchemaResponse>;
  profileId: string;
  dialect: string;
}

export function SchemaExplorerPanel({
  apiFetch,
  profileId,
  dialect,
}: SchemaExplorerProps) {
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedTables, setExpandedTables] = useState<Record<string, boolean>>({});

  useEffect(() => {
    loadSchema();
  }, [profileId, dialect]);

  async function loadSchema() {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch(`/connections/profiles/${profileId}/schema`, {
        method: "GET",
      });
      setSchema(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  const toggleTable = (tableName: string) => {
    setExpandedTables(prev => ({
      ...prev,
      [tableName]: !prev[tableName],
    }));
  };

  if (loading) {
    return <div className="schema-explorer" style={{ padding: "16px" }}>
      <div className="skeleton-loader">
        <div className="skeleton" style={{ width: "80%" }} />
        <div className="skeleton" style={{ width: "60%" }} />
        <div className="skeleton" style={{ width: "40%" }} />
      </div>
    </div>;
  }

  if (error) {
    return (
      <div className="error-banner" style={{ padding: "12px", margin: "8px 0" }}>
        Error: {error}
      </div>
    );
  }

  return (
    <div className="schema-explorer">
      <h3>Database Schema</h3>
      <div className="schema-tree">
        {schema?.tables ? (
          Object.entries(schema.tables).map(([tableName, table]) => (
            <div key={tableName} className="table-item">
              <div className="table-header" onClick={() => toggleTable(tableName)}>
                <strong>{tableName}</strong>
                <span className="expand-icon">{expandedTables[tableName] ? "▾" : "▸"}</span>
              </div>
              {expandedTables[tableName] && table.columns && (
                <div className="table-section">
                  <h4>Columns</h4>
                  <table className="schema-table">
                    <thead>
                      <tr>
                        <th>Column</th>
                        <th>Type</th>
                        <th>Constraints</th>
                      </tr>
                    </thead>
                    <tbody>
                      {table.columns.map((col, i) => (
                        <tr key={i}>
                          <td>{String(col.name)}</td>
                          <td>{String(col.type)}</td>
                          <td>
                            {Boolean(col.primary) && "PK "}
                            {Boolean(col.auto) && "AUTO "}
                            {Boolean(col.unique) && "UNIQUE "}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {expandedTables[tableName] && table.indexes && (
                <div className="table-section">
                  <h4>Indexes</h4>
                  <table className="schema-table">
                    <thead>
                      <tr>
                        <th>Index Name</th>
                        <th>Columns</th>
                      </tr>
                    </thead>
                    <tbody>
                      {table.indexes.map((idx, i) => (
                        <tr key={i}>
                          <td>{String(idx.name)}</td>
                          <td>
                            {Array.isArray(idx.columns) ? idx.columns.map((col, ci) => (
                              <span key={ci}>{String((col as Record<string, unknown>).name)}{Boolean(idx.unique) && " (UNIQUE)" ? " " : ""}</span>
                            )) : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {expandedTables[tableName] && table.foreign_keys && (
                <div className="table-section">
                  <h4>Foreign Keys</h4>
                  <table className="schema-table">
                    <thead>
                      <tr>
                        <th>FK Name</th>
                        <th>Table</th>
                        <th>Columns</th>
                        <th>Reference</th>
                      </tr>
                    </thead>
                    <tbody>
                      {table.foreign_keys.map((fk, i) => (
                        <tr key={i}>
                          <td>{String(fk.name)}</td>
                          <td>{String(fk.reference_table)}</td>
                          <td>{Array.isArray(fk.columns) ? fk.columns.join(", ") : "—"}</td>
                          <td>{Array.isArray(fk.reference_columns) ? fk.reference_columns.join(", ") : "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          ))
        ) : (
          <div className="empty-state">No tables found</div>
        )}
      </div>
    </div>
  );
}