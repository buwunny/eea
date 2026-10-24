import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
const bridge = process.env.EEA_BRIDGE_URL || "http://127.0.0.1:3001";
export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.EEA_GUI_PORT || 5173),
    strictPort: true,
    proxy: {
      "/api": bridge,
      "/artifacts": bridge,
      "/ws": { target: bridge.replace(/^http/, "ws"), ws: true },
    },
  },
});
