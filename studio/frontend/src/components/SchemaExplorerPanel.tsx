import React, { useState, useEffect } from "react";
import { apiFetch } from "./apiFetch";

interface SchemaItem {
  name: string;
  type: string;
  columns: any[];
  indexes: any[];
  foreign_keys: any[];
}

interface SchemaExplorerProps {
  apiFetch: (path: string, init?: RequestInit) => Promise<any>;
  profileId: string;
  dialect: string;
}

export function SchemaExplorerPanel({
  apiFetch,
  profileId,
  dialect,
}: SchemaExplorerProps) {
  const [schema, setSchema] = useState<any>(null);
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
      setError(e.message);
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
                      {table.columns.map((col: any, i: number) => (
                        <tr key={i}>
                          <td>{col.name}</td>
                          <td>{col.type}</td>
                          <td>
                            {col.primary ? "PK " : ""}
                            {col.auto ? "AUTO " : ""}
                            {col.unique ? "UNIQUE " : ""}
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
                      {table.indexes.map((idx: any, i: number) => (
                        <tr key={i}>
                          <td>{idx.name}</td>
                          <td>
                            {idx.columns.map((col: any, ci: number) => (
                              <span key={ci}>{col.name}{idx.unique && " (UNIQUE)" ? " " : ""}</span>
                            ))}
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
                      {table.foreign_keys.map((fk: any, i: number) => (
                        <tr key={i}>
                          <td>{fk.name}</td>
                          <td>{fk.reference_table}</td>
                          <td>{fk.columns.join(", ")}</td>
                          <td>{fk.reference_columns.join(", ")}</td>
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