import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev proxy keeps dev and prod origin-identical (docs/09): "/api" is forwarded
// to the FastAPI backend with the path preserved, so the client can use a
// relative base of /api/v1 everywhere.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: false,
      },
    },
  },
});
