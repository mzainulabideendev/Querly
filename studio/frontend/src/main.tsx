import React from "react";
import { createRoot } from "react-dom/client";
import "./index.css";

import App from "./App";

declare global {
  interface ImportMetaEnv {
    readonly VITE_BACKEND_URL?: string;
  }
  interface ImportMeta {
    readonly env: ImportMetaEnv;
  }
}

// The FastAPI backend serves the built frontend at the same origin, so relative
// URLs are correct for both the packaged desktop app and `vite preview`. During
// `npm run dev` Vite proxies these prefixes to the backend (see vite.config.ts).
const backendBase = (import.meta.env.VITE_BACKEND_URL ?? "").replace(/\/+$/, "");

export async function apiFetch(path: string, init: RequestInit = {}) {
  const response = await fetch(`${backendBase}${path}`, {
    credentials: "include",
    ...init,
    headers: {
      Accept: "application/json",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    const text = await response.text();
    if (text) {
      try {
        const parsed = JSON.parse(text);
        detail =
          typeof parsed.detail === "string"
            ? parsed.detail
            : JSON.stringify(parsed.detail ?? parsed);
      } catch {
        detail = text;
      }
    }
    throw new Error(detail);
  }

  if (response.status === 204) {
    return null;
  }
  return response.json();
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App apiFetch={apiFetch} />
  </React.StrictMode>,
);
