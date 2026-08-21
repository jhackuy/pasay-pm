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

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Canonical production runtime mode. Hard-coded in the image so a
    # forgotten env var cannot silently fall back to dev behaviour.
    PASAY_RUNTIME_MODE=cloudflare-container

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
 && chown -R appuser:appgroup /app/uploads

USER appuser:appgroup

# ── Startup ────────────────────────────────────────────────────────────────
# Step ordering per Scope D + ND_RETURN FIX1 blocker #4 fail-fast contract:
#   1. REQUIRE DATABASE_URL_UNPOOLED — NO silent fallback to DATABASE_URL.
#      Missing env → container startup fails IMMEDIATELY with a clear error.
#      (Scope E: migrations use direct/unpooled; Scope D: fail-fast on missing
#      required env; ND_RETURN FIX1 blocker #4 explicitly forbids fallback.)
#   2. alembic upgrade head using the direct/unpooled Neon URL.
#   3. exec uvicorn app.main:app — HTTP-only, NO polling loops.
#
# If migrations fail the whole container fails immediately (entrypoint is
# `sh -e` by default; non-zero alembic exit propagates).
ENTRYPOINT ["sh", "-c", "\
set -e; \
if [ -z \"${DATABASE_URL_UNPOOLED}\" ]; then \
  echo '[pasay][fatal] DATABASE_URL_UNPOOLED is required for alembic migrations (Scope E direct/unpooled + ND_RETURN FIX1 blocker #4: NO fallback). Container cannot start.' >&2; \
  exit 1; \
fi; \
export ALEMBIC_DATABASE_URL=\"${DATABASE_URL_UNPOOLED}\"; \
alembic upgrade head; \
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
