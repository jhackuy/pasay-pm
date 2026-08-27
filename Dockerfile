# ────────────────────────────────────────────────────────────────────────────
# PASAY-TASK-011 / Production Architecture Closeout P0 — Dockerfile
# Target runtime: Cloudflare Container (single Python runtime)
#
# Invariants (Scope D + Long Polling Exit Gate):
#   • ONE FastAPI app, ONE DB boundary, ONE Telegram processing service.
#   • NEVER calls run_polling() / getUpdates.
#   • Fail-fast startup: required env missing → health degraded → container
#     reports unhealthy; operator intervention required.
#   • alembic migrations use DATABASE_URL_UNPOOLED (direct connection, Scope E).
#   • application runtime uses DATABASE_URL (pooled, Neon-recommended).
# ────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

ARG GITHUB_SHA=""
ENV GITHUB_SHA_ARG=${GITHUB_SHA}

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Canonical production runtime mode. Hard-coded in the image so a
    # forgotten env var cannot silently fall back to dev behaviour.
    PASAY_RUNTIME_MODE=cloudflare-container \
    # Build identity (Cloudflare Container runtime constructor may override
    # PASAY_BUILD_SHA at boot; Docker-level ARG fallback is set HERE so the
    # container can always report a build identity to /health even when the
    # constructor injection misses.)
    PASAY_BUILD_SHA=${GITHUB_SHA}

# ── System deps (psycopg2 build-time only, minimal runtime) ──
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
 && rm -rf /var/lib/apt/lists/*

# ── App sources ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App sources ──
COPY . .

RUN mkdir -p /app/uploads

# ── Non-root runtime user (security best practice) ──
# Create a dedicated unprivileged `app` user + group.  Writability of
# /app/uploads is preserved by chown so uploads still work at runtime.
# The rest of /app stays root-owned (read-only for the app user).
RUN groupadd --system --gid 10001 appgroup \
 && useradd  --system --uid 10000 --gid appgroup --create-home --shell /usr/sbin/nologin appuser \
 && mkdir -p /app/state \
 && chown -R appuser:appgroup /app/uploads /app/state

USER appuser:appgroup

# ── Startup ────────────────────────────────────────────────────────────────
# M006 RETURN3-B fix: container no longer runs alembic at boot.
# Production migrations are run EXCLUSIVELY as a gated CI step before Worker
# deploy (pasay-deploy-phase1.yml STEP 3 — production migration gate).
# This removes the slow pre-port startup window that triggered the Cloudflare
# containers upstream #232 lifecycle false-negative during readiness probing.
#
# Fail-fast contract preserved here:
#   1. DATABASE_URL_UNPOOLED env presence is still required at container boot.
#      (Worker injects this secret; missing env -> exit 1 immediately.)
#   2. exec uvicorn app.main:app — HTTP-only, NO polling loops.
#
# The Container runtime NEVER calls alembic; migration truth must be proven in
# CI BEFORE deploy; `BLOCKED_PRODUCTION_MIGRATION` aborts the whole workflow.
ENTRYPOINT ["sh", "-c", "\
set -e; \
if [ -z \"${DATABASE_URL_UNPOOLED}\" ]; then \
  echo '[pasay][fatal] DATABASE_URL_UNPOOLED is required (Scope E direct/unpooled secret presence gate). Container cannot start.' >&2; \
  exit 1; \
fi; \
unset DATABASE_URL_UNPOOLED; \
exec \"$@\"\
", "entrypoint"]

# ── Production: HTTP server ONLY — long polling is EXPLICITLY absent. ──
# Long Polling Exit Gate proof (T7):
#   • No `bin/pasay_runtime.py` in CMD.
#   • No `python -m pasay_bot.main run_polling`.
#   • No subprocess shell-out that could spawn a getUpdates loop.
# Scope D explicitly requires: "不在 Container 内启动 PTB long polling".
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
