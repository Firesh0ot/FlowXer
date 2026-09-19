import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

function mixerProxy() {
  const token = process.env.FLOWXER_API_TOKEN || "";
  return {
    target: "http://127.0.0.1:9610",
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

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 9620,
    proxy: {
      "/api": mixerProxy(),
      "/docs": mixerProxy(),
      "/redoc": mixerProxy(),
      "/graphics": mixerProxy(),
      "/openapi.json": mixerProxy(),
    },
  },
  preview: {
    host: "0.0.0.0",
    port: 9620,
  },
});
