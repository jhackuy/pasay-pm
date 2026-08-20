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

# ── Python deps ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── App sources ──
COPY . .

RUN mkdir -p /app/uploads

# ── Startup ────────────────────────────────────────────────────────────────
# Step ordering per Scope D fail-fast contract:
#   1. alembic upgrade head using **direct/unpooled** Neon URL
#      (operator sets DATABASE_URL_UNPOOLED; if empty we fall back to
#      DATABASE_URL only for the migration step but log a warning).
#   2. exec uvicorn app.main:app — HTTP-only, NO polling loops.
#
# If migrations fail the whole container fails immediately (entrypoint is
# `sh -e` by default; non-zero alembic exit propagates).
ENTRYPOINT ["sh", "-c", "\
set -e; \
MIGRATION_URL=\"${DATABASE_URL_UNPOOLED:-${DATABASE_URL}}\"; \
if [ -z \"${DATABASE_URL_UNPOOLED}\" ]; then \
  echo '[pasay][warn] DATABASE_URL_UNPOOLED not set; using DATABASE_URL for alembic (Scope E recommends separate direct URL for migrations).'; \
fi; \
export ALEMBIC_DATABASE_URL=\"$MIGRATION_URL\"; \
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
