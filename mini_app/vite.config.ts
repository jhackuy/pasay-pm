/** PASAY Mini App — Vite build configuration.
 *
 *  Single-page hash-routed Mini App. The build emits `dist/index.html`
 *  plus a hashed `dist/assets/*` bundle, exactly what `tests/smoke.ts`
 *  asserts (see `dist build artifacts contain view modules`).
 *
 *  No bundler-time magic, no extra plugins: just the default Vite TS/ESM
 *  build. Production deployment is expected to serve `dist/` from a CDN
 *  or from the FastAPI container.
 */
import { defineConfig } from "vite";

export default defineConfig({
  root: ".",
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
    rollupOptions: {
      output: {
        // Single chunk by default; Vite will split if a chunk exceeds the
        // default 500 kB limit, which our code never does.
        manualChunks: undefined,
      },
    },
  },
  server: {
    port: 5173,
    strictPort: false,
  },
});
