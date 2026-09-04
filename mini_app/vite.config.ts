/** PASAY Mini App — Vite build configuration.
 *
 *  Single-page hash-routed Mini App. The build emits `dist/index.html`
 *  plus a hashed `dist/assets/*` bundle, exactly what `tests/smoke.ts`
 *  asserts (see `dist build artifacts contain view modules`).
 *
 *  No bundler-time magic, no extra plugins: just the default Vite TS/ESM
 *  build. Production deployment is expected to serve `dist/` from a CDN
 *  (Cloudflare Pages, see mini_app/wrangler.toml) or from the FastAPI
 *  container (the SPA fallback in app/v1/main.py still applies).
 */
import { defineConfig } from "vite";

export default defineConfig({
  root: ".",
  // Issue #119: ``./`` keeps the asset URLs relative so the bundle
  // works both at the Cloudflare Pages root (https://pasay-mini-app.pages.dev/)
  // AND behind the FastAPI container path / Mini App mount — absolute
  // paths would break the second deployment shape.
  base: "./",
  // Issue #119: mirror every entry under `public/` (notably `_redirects`)
  // into the build output so Cloudflare Pages picks up the SPA fallback
  // without a custom Pages Function.
  publicDir: "public",
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
