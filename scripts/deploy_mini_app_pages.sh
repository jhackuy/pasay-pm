#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────────────────────
# PASAY Mini App — Cloudflare Pages publish (Issue #119 trusted-lane).
#
# This script is the trusted production publication path for mini_app/dist.
# It may be invoked by the production deploy workflow or manually by an
# operator that already has the Cloudflare production credentials in scope.
#
# Steps:
#   1. Verify prerequisites and required Cloudflare environment.
#   2. Install the exact locked Mini App dependencies with npm ci.
#   3. Re-run `npm run build` so `dist/` matches the latest TypeScript.
#   4. Run the JSDOM smoke gate so a broken bundle cannot ship.
#   5. Invoke `wrangler pages deploy dist --project-name=pasay-mini-app`.
#
# Required environment:
#   CLOUDFLARE_API_TOKEN    — Pages edit scope
#   CLOUDFLARE_ACCOUNT_ID   — Pages project owner
#
# Exit codes:
#   0 = published (or already published) successfully
#   non-zero = failed at any gate; publish is not attempted after a failed gate.
# ────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd)"
MINI_APP_DIR="${REPO_ROOT}/mini_app"
DIST_DIR="${MINI_APP_DIR}/dist"
PROJECT_NAME="pasay-mini-app"
CANONICAL_URL="https://${PROJECT_NAME}.pages.dev"

log() { printf '[deploy:pages] %s\n' "$*" >&2; }
die() { printf '[deploy:pages][FATAL] %s\n' "$*" >&2; exit 1; }

# ── Gate 0: prerequisites ─────────────────────────────────────────────────
command -v node  >/dev/null 2>&1 || die "node is required on PATH"
command -v npm   >/dev/null 2>&1 || die "npm is required on PATH"
command -v npx   >/dev/null 2>&1 || die "npx is required on PATH"
[ -n "${CLOUDFLARE_API_TOKEN:-}" ]  || die "CLOUDFLARE_API_TOKEN env var is required"
[ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ] || die "CLOUDFLARE_ACCOUNT_ID env var is required"
[ -f "${MINI_APP_DIR}/package.json" ] || die "mini_app/package.json missing under ${MINI_APP_DIR}"
[ -f "${MINI_APP_DIR}/package-lock.json" ] || die "mini_app/package-lock.json missing — reproducible deploy unavailable"
[ -f "${MINI_APP_DIR}/wrangler.toml" ] || die "mini_app/wrangler.toml missing — Issue #119 Pages config absent"
[ -f "${MINI_APP_DIR}/public/_redirects" ] || die "mini_app/public/_redirects missing — Pages SPA fallback absent"

# ── Gate 1: locked dependencies ───────────────────────────────────────────
# GitHub setup-node cache does not populate node_modules. Production deploys
# must explicitly materialize the lockfile-pinned dependency tree before tsc.
log "Installing locked Mini App dependencies"
( cd "${MINI_APP_DIR}" && npm ci --no-audit --no-fund )

# ── Gate 2: build the bundle the operator is about to ship ────────────────
log "Rebuilding mini_app/dist from current TypeScript sources"
( cd "${MINI_APP_DIR}" && npm run build )

[ -f "${DIST_DIR}/index.html" ] || die "build did not produce ${DIST_DIR}/index.html"
[ -d "${DIST_DIR}/assets" ]     || die "build did not produce ${DIST_DIR}/assets/"
[ -f "${DIST_DIR}/_redirects" ]  || die "build did not copy public/_redirects into dist/ — Vite publicDir missing?"

# ── Gate 3: JSDOM smoke gate (fail-closed: broken bundle never ships) ─────
log "Running JSDOM smoke gate against the rebuilt bundle"
( cd "${MINI_APP_DIR}" && npm run test:smoke )

# ── Gate 4: publish to Cloudflare Pages ───────────────────────────────────
log "Publishing to Cloudflare Pages project '${PROJECT_NAME}'"
(
  cd "${MINI_APP_DIR}"
  CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN}" \
  CLOUDFLARE_ACCOUNT_ID="${CLOUDFLARE_ACCOUNT_ID}" \
    npx --yes wrangler@3 pages deploy dist \
      --project-name "${PROJECT_NAME}" \
      --commit-dirty=true \
      --branch main
)

log "Publish OK. Canonical production URL: ${CANONICAL_URL}"
exit 0
