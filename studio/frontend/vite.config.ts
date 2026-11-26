import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = "http://127.0.0.1:8765";

// Keep these prefixes in sync with studio/backend/app.py.
const proxy = {
  "/health": { target: backend, changeOrigin: true },
  "/compiler": { target: backend, changeOrigin: true },
  "/connections": { target: backend, changeOrigin: true },
  "/execution": { target: backend, changeOrigin: true },
  "/converter": { target: backend, changeOrigin: true },
};

export default defineConfig({
  plugins: [react()],
  server: {
    port: 1420,
    strictPort: true,
    proxy,
  },
  optimizeDeps: {
    include: ["@monaco-editor/react"],
  },
});
