import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: { input: { index: resolve(import.meta.dirname, "app.html") } }
  },
  server: {
    port: 5173,
    // Keep the React development UI wired to the same local API as the
    // production single-page server on port 8900.
    proxy: { "/api": "http://127.0.0.1:8900" }
  }
});
