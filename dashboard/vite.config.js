import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// The Ryu REST API (controller.py) runs on :8080. Proxy /api to it so the
// browser talks to the dashboard origin only (no CORS needed).
export default defineConfig({
    plugins: [react()],
    server: {
        port: 5173,
        proxy: {
            "/api": { target: "http://localhost:8080", changeOrigin: true },
        },
    },
});
