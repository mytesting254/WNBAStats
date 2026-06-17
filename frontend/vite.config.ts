import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const frontendHost =
  process.env.FRONTEND_HOST ??
  (process.env.CODESPACES === "true" ? "0.0.0.0" : "127.0.0.1");

export default defineConfig({
  plugins: [react()],
  server: {
    host: frontendHost,
    port: Number(process.env.FRONTEND_PORT ?? 5184),
    strictPort: true,
    proxy: {
      "/api": process.env.VITE_BACKEND_URL ?? "http://127.0.0.1:8010"
    }
  },
  preview: {
    host: frontendHost,
    port: Number(process.env.FRONTEND_PORT ?? 5184),
    strictPort: true
  }
});
