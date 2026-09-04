import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    // jsdom for the URL helpers, which read and write window.location and
    // window.history. The routing itself is tested against two engines in
    // Python; what is worth covering here is the browser-shaped logic that
    // has no oracle.
    environment: "jsdom",
    include: ["src/**/*.test.ts"],
  },
  build: {
    rollupOptions: {
      output: {
        // MapLibre is ~700 kB of the bundle and changes about once a year,
        // while the app changes constantly. Splitting it means a deploy
        // invalidates ~40 kB of cache rather than all of it.
        manualChunks: {
          maplibre: ["maplibre-gl"],
          react: ["react", "react-dom"],
        },
      },
    },
  },
  server: {
    port: 5174,
    // Proxy /api to the backend so the browser sees one origin in development
    // and CORS never enters the picture locally.
    proxy: {
      "/api": {
        target: "http://localhost:8001",
        changeOrigin: true,
        // ws so /api/ws/vehicles upgrades through the proxy rather than
        // 404ing; without it the live map silently never connects.
        ws: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
