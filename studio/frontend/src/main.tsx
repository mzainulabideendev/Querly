import React from "react";
import { createRoot } from "react-dom";
import { RouterProvider, useRoutes } from "react-router-dom";
import "./index.css";

import App from "./App";

// Tauri bridge - if available, we can use window.__TAURI__.invoke
// Otherwise the frontend communicates with the FastAPI backend via relative URLs

// Simple service wrapper for backend API
const backendBase = import.meta.env.VITE_BACKEND_URL || "/api";

async function apiFetch(path: string, init: RequestInit = {}) {
  const resp = await fetch(`${backendBase}${path}`, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...init.headers,
    },
    ...init,
  });
  if (!resp.ok) {
    const err = await resp.text();
    throw new Error(`API error ${resp.status}: ${err}`);
  }
  return resp.json();
}

// Component will use apiFetch to talk to the backend

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App apiFetch={apiFetch} backendBase={backendBase} />
  </React.StrictMode>
);