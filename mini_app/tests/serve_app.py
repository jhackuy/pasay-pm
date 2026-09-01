"""Browser-smoke test harness for the PASAY V1 rewrite.

Boots a disposable SQLite-backed FastAPI app on a free local port, mounts
the built Mini App at `/`, prints the URL once the server is ready, then
waits for SIGTERM/SIGINT to shut down cleanly.

Why SQLite in this harness instead of PostgreSQL?
- The `build-core-smoke` CI job intentionally has no DB service. This
  harness runs in that job.
- The V1 ORM is portable across SQLite and PostgreSQL (BigInteger, Numeric,
  CHECK constraints, partial uniques are all mapped); the rewrite tests
  use the same metadata.
- Money is still NUMERIC(14, 2)/Decimal on both backends, so the
  constitutional invariant holds end-to-end.

Why does this script bind to a free port?
- CI runners may have port 8000 occupied by leftover processes. We bind
  to 0 (OS-chosen), then surface the bound port on stdout.

Usage:
    python tests/serve_app.py <path-to-mini-app-dist>

Output contract (consumed by `run_browser_smoke.mjs`):
    Line 1 (stdout): ``READY_URL=http://127.0.0.1:<port>``
    Line 2 (stdout): ``READY_DIST=<abs path to dist>``

Any failure during startup (dist not found, port bind error, schema
create error, etc.) is printed to stderr and exits with a non-zero code.
"""
from __future__ import annotations

import contextlib
import os
import signal
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Import only after sys.path is corrected so app.* resolves cleanly.
import uvicorn  # noqa: E402

from app.v1.models.base import V1Base  # noqa: E402
import app.v1.models  # noqa: E402,F401  # ensure all V1 tables register on V1Base.metadata


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_ready(port: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError):
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        time.sleep(0.05)
    return False


def _build_app_with_miniapp(dist_abs: Path):
    """Return a FastAPI app with the Mini App mounted and a fresh SQLite DB."""
    # Late import — keeps startup fast on the error path.
    from fastapi import FastAPI

    from app.db import session as db_session
    from app.v1.main import create_v1_app

    tmp = tempfile.NamedTemporaryFile(
        prefix="pasay_browser_smoke_", suffix=".sqlite", delete=False
    )
    tmp.close()
    db_url = f"sqlite:///{tmp.name}"

    # Set the env contract BEFORE create_v1_app so the optional Mini App
    # mount sees PASAY_MINIAPP_DIST and registers the SPA fallback in the
    # single app instance that uvicorn will run.
    os.environ["DATABASE_URL"] = db_url
    os.environ["PASAY_MINIAPP_DIST"] = str(dist_abs)
    db_session.bind_engine(db_url)

    # Create the schema in the fresh SQLite file. We deliberately do NOT
    # run alembic here — the browser-smoke gate is about the *runtime*
    # Mini App + V1 API contract, not about the migration chain (the
    # `fresh-postgres-alembic` gate covers that).
    from sqlalchemy import create_engine as _ce, event
    from sqlalchemy.orm import sessionmaker as _sm

    engine = _ce(db_url, future=True)

    # SQLite does not auto-increment BigInteger PKs the same way PostgreSQL
    # does for BIGSERIAL. The V1 ORM declares every PK as `BigInteger`,
    # so on SQLite the column is NOT NULL with no autoincrement. We attach
    # a `before_flush` listener on the session factory so every session
    # opened by FastAPI's get_db sees a NULL primary key on a newly-added
    # instance and assigns the next value from a per-process counter.
    # PostgreSQL behavior (BIGSERIAL autoincrement) is unaffected.
    _sqlite_pk_counter = {"n": 0}

    sqlite_sm = _sm(bind=engine, autoflush=False, autocommit=False, future=True)

    @event.listens_for(sqlite_sm, "before_flush")
    def _sqlite_autopk(session, flush_context, instances):  # noqa: ARG001
        for obj in session.new:
            pk_attr = inspect_primary_key(obj)
            if pk_attr is None:
                continue
            current = getattr(obj, pk_attr, None)
            if current is None:
                _sqlite_pk_counter["n"] += 1
                setattr(obj, pk_attr, _sqlite_pk_counter["n"])

    V1Base.metadata.create_all(engine)
    engine.dispose()

    # Replace the global session factory so FastAPI's get_db() routes
    # through our PK-filling session. This is harness-local: the rewrite
    # tests in pytest use the same bind_engine path but bind to their own
    # engine and never touch the harness session factory.
    db_session._session_factory = sqlite_sm  # type: ignore[attr-defined]
    db_session._engine = engine  # type: ignore[attr-defined]
    db_session._bound_url = db_url  # type: ignore[attr-defined]

    app = create_v1_app()
    return app, tmp.name


def inspect_primary_key(obj) -> str | None:
    """Return the name of the single primary-key attribute on obj, or None."""
    from sqlalchemy import inspect

    insp = inspect(obj)
    pk_cols = insp.mapper.primary_key
    if len(pk_cols) != 1:
        return None
    return pk_cols[0].key


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: serve_app.py <mini_app_dist>", file=sys.stderr)
        return 2

    dist = Path(argv[1]).resolve()
    if not (dist / "index.html").is_file():
        print(f"dist/index.html missing under {dist}", file=sys.stderr)
        return 3

    port = _pick_free_port()
    app, db_file = _build_app_with_miniapp(dist)

    config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)

    stop_event = threading.Event()

    def _signal_handler(signum, frame):  # noqa: ARG001
        stop_event.set()
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _signal_handler)
        except (ValueError, OSError):
            pass

    server_thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    server_thread.start()

    if not _wait_for_ready(port, timeout=15.0):
        print(f"server did not become ready on port {port}", file=sys.stderr)
        server.should_exit = True
        server_thread.join(timeout=5.0)
        return 4

    url = f"http://127.0.0.1:{port}"
    sys.stdout.write(f"READY_URL={url}\n")
    sys.stdout.write(f"READY_DIST={dist}\n")
    sys.stdout.write(f"READY_DB={db_file}\n")
    sys.stdout.flush()

    try:
        while not stop_event.is_set():
            time.sleep(0.1)
    finally:
        server.should_exit = True
        server_thread.join(timeout=5.0)
        with contextlib.suppress(OSError):
            os.unlink(db_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
