import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

const backendTarget = process.env.MPGUI_BACKEND_URL || "http://127.0.0.1:28765";

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      "/api": {
        target: backendTarget,
        changeOrigin: true
      },
      "/health": {
        target: backendTarget,
        changeOrigin: true
      }
    }
  }
});
