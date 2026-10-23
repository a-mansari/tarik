// The team's pages, built into server/web/dist and served by server/server.py under /ui/ (npm run build). `npm run dev` serves
// them on :5173, the data still from the server on :2024.
import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

const server = "http://127.0.0.1:2024";

export default defineConfig({
  base: "/ui/",
  plugins: [vue()],
  build: { outDir: "dist", emptyOutDir: true, target: "es2022" },
  server: { proxy: { "/ui/api": server, "/ui/live": server, "/runs": server, "/graphs": server, "/prompts": server } },
});
