import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 9620,
    proxy: {
      "/api": "http://127.0.0.1:9610",
      "/docs": "http://127.0.0.1:9610",
      "/redoc": "http://127.0.0.1:9610",
      "/graphics": "http://127.0.0.1:9610",
      "/openapi.json": "http://127.0.0.1:9610",
    },
  },
  preview: {
    host: "0.0.0.0",
    port: 9620,
  },
});
