import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dashboard is a thin client of the same API the CLI uses. In dev, Vite
// proxies to the local core so the browser never needs to know the port or
// fight CORS; in production the built assets are served by FastAPI itself.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true,
      },
      "/health": {
        target: "http://127.0.0.1:8765",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
