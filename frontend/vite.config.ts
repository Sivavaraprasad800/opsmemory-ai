import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * The API is proxied rather than called cross-origin so the browser sees one origin in
 * development. Change `VITE_API_TARGET` to point at a backend that is not on the default port.
 */
const target = process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
