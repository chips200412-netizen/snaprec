import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { shareTransportPlugin } from "./share-transport-plugin.ts";

export default defineConfig({
  cacheDir: "../var/vite-cache/web",
  plugins: [shareTransportPlugin(), react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: true,
  },
});
