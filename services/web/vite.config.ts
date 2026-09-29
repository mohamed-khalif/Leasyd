import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { mockApi } from "./mock/mockApi";

// `npm run mock` serves the app with a fake /v1/app backend and no sign-in,
// for working on the UI without AWS.
export default defineConfig(({ mode }) => ({
  plugins: [react(), ...(mode === "mock" ? [mockApi()] : [])],
  build: { outDir: "dist", sourcemap: false },
}));
