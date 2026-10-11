import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

function mixerProxy() {
  const token = process.env.FLOWXER_API_TOKEN || "";
  return {
    target: process.env.FLOWXER_MIXER_URL || "http://127.0.0.1:9610",
    configure(proxy: { on: (event: string, fn: (...args: any[]) => void) => void }) {
      if (!token) {
        return;
      }
      proxy.on("proxyReq", (proxyReq: { setHeader: (name: string, value: string) => void }) => {
        proxyReq.setHeader("Authorization", `Bearer ${token}`);
      });
    },
  };
}

const guiPort = Number(process.env.FLOWXER_GUI_PORT || 9620);

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: guiPort,
    proxy: {
      "/api": mixerProxy(),
      "/docs": mixerProxy(),
      "/redoc": mixerProxy(),
      "/graphics": mixerProxy(),
      "/openapi.json": mixerProxy(),
      // nginx serves the widget list at /widgets (dev: the page is /widget.html?me=1).
      "/widgets": { ...mixerProxy(), rewrite: () => "/api/v1/widgets" },
    },
  },
  build: {
    rollupOptions: {
      // The console, and the bus widget nginx serves at /widget/bus.
      input: {
        main: fileURLToPath(new URL("index.html", import.meta.url)),
        widget: fileURLToPath(new URL("widget.html", import.meta.url)),
      },
    },
  },
  preview: {
    host: "0.0.0.0",
    port: guiPort,
  },
});
