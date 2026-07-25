import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../static",
    emptyOutDir: true,
  },
  server: {
    port: 5177,
    strictPort: true,
  },
  preview: {
    port: 4177,
    strictPort: true,
  },
});
