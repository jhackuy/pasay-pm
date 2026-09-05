#!/usr/bin/env bash
# PASAY Mini App — Cloudflare Pages publish (Issue #119 trusted-lane).
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." &> /dev/null && pwd)"
MINI_APP_DIR="${REPO_ROOT}/mini_app"
DIST_DIR="${MINI_APP_DIR}/dist"
PROJECT_NAME="pasay-mini-app"
CANONICAL_URL="https://${PROJECT_NAME}.pages.dev"
WORKER_NAME="pasay-cloudflare-worker"

log() { printf '[deploy:pages] %s\n' "$*" >&2; }
die() { printf '[deploy:pages][FATAL] %s\n' "$*" >&2; exit 1; }

# Gate 0: prerequisites
command -v node >/dev/null 2>&1 || die "node is required on PATH"
command -v npm  >/dev/null 2>&1 || die "npm is required on PATH"
command -v npx  >/dev/null 2>&1 || die "npx is required on PATH"
[ -n "${CLOUDFLARE_API_TOKEN:-}" ] || die "CLOUDFLARE_API_TOKEN env var is required"
[ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ] || die "CLOUDFLARE_ACCOUNT_ID env var is required"
[ -f "${MINI_APP_DIR}/package.json" ] || die "mini_app/package.json missing under ${MINI_APP_DIR}"
[ -f "${MINI_APP_DIR}/package-lock.json" ] || die "mini_app/package-lock.json missing — reproducible deploy unavailable"
[ -f "${MINI_APP_DIR}/wrangler.toml" ] || die "mini_app/wrangler.toml missing — Issue #119 Pages config absent"
[ -f "${MINI_APP_DIR}/public/_redirects" ] || die "mini_app/public/_redirects missing — Pages SPA fallback absent"

# Issue #119 Mini App production wiring: discover the Cloudflare account
# workers subdomain and bake ``VITE_API_ROOT`` into the build so the SPA's
# PasayClient targets the Worker ``/api/v1/*`` proxy (the Worker forwards to
# the Container's FastAPI V1 surface; see cloudflare-worker/src/index.ts).
# ``pages.dev`` is static and cannot serve ``/api/v1/*``, and the Container
# itself is NOT publicly addressable, so the Worker is the only reachable
# hostname for the SPA's authenticated requests.
log "Resolving Cloudflare workers subdomain for VITE_API_ROOT"
WORKERS_SUBDOMAIN="$(
  curl -fsS \
    -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
    -H "Content-Type: application/json" \
    "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/workers/subdomain" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); sub=(d.get("result") or {}).get("subdomain") or ""; print(sub)'
)"
[ -n "${WORKERS_SUBDOMAIN}" ] || die "Cloudflare API did not return a workers subdomain for the account"
export VITE_API_ROOT="https://${WORKER_NAME}.${WORKERS_SUBDOMAIN}.workers.dev/api/v1"
log "VITE_API_ROOT=${VITE_API_ROOT}"

# Gate 1: locked dependencies
log "Installing locked Mini App dependencies"
( cd "${MINI_APP_DIR}" && npm ci --no-audit --no-fund )

# Gate 2: build
log "Rebuilding mini_app/dist from current TypeScript sources"
( cd "${MINI_APP_DIR}" && npm run build )
[ -f "${DIST_DIR}/index.html" ] || die "build did not produce ${DIST_DIR}/index.html"
[ -d "${DIST_DIR}/assets" ] || die "build did not produce ${DIST_DIR}/assets/"
[ -f "${DIST_DIR}/_redirects" ] || die "build did not copy public/_redirects into dist/ — Vite publicDir missing?"

# Gate 3: JSDOM smoke
log "Running JSDOM smoke gate against the rebuilt bundle"
( cd "${MINI_APP_DIR}" && npm run test:smoke )

# Gate 4: ensure the Direct Upload Pages project exists.
# Wrangler v3 `pages project list` does not support --json, so use its stable
# human-readable output only as an existence probe. Creation remains idempotent.
log "Ensuring Cloudflare Pages project '${PROJECT_NAME}' exists"
PROJECTS_LIST="$(
  cd "${MINI_APP_DIR}"
  CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN}" \
  CLOUDFLARE_ACCOUNT_ID="${CLOUDFLARE_ACCOUNT_ID}" \
    npx --yes wrangler@3 pages project list
)"
if ! printf '%s\n' "${PROJECTS_LIST}" | grep -Fq "${PROJECT_NAME}"; then
  log "Pages project missing; creating '${PROJECT_NAME}' with production branch main"
  (
    cd "${MINI_APP_DIR}"
    CLOUDFLARE_API_TOKEN="${CLOUDFLARE_API_TOKEN}" \
    CLOUDFLARE_ACCOUNT_ID="${CLOUDFLARE_ACCOUNT_ID}" \
      npx --yes wrangler@3 pages project create "${PROJECT_NAME}" --production-branch main
  )
fi

# Gate 5: publish
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
