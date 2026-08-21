import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// Served by FastAPI at /console/ (built SPA in static/console/dist/) with
// html=True. Base must match so the built index.html references
// /console/assets/... correctly.
export default defineConfig({
  plugins: [vue()],
  base: "/console/",
  build: {
    outDir: "../doctoragent/api/static/console/dist",
    emptyOutDir: true,
    assetsDir: "assets",
  },
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/console/api": "http://localhost:8000",
    },
  },
});
