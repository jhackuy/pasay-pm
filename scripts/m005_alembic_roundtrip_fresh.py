"""Create a fresh throwaway PG16 database and run alembic roundtrip.

Fresh DB creation: if current user is superuser on localhost PG, we
CREATE DATABASE directly; otherwise it still runs on the user's existing
dev DB but pre-cleans it (DROP SCHEMA public CASCADE + CREATE SCHEMA public)
which is equivalent for proving the last-3 upgrade→downgrade→upgrade chain.

Exit codes:
  0: overall_roundtrip_ok=true
  1: any failure
Prints JSON evidence to stdout.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import settings  # noqa: E402

FINAL_3_REV_ORDER = ["m4b000000001", "m4c000000001", "m4d000000001"]

# Fixed down_revision values extracted verbatim from each migration's
# `down_revision` module attribute (source of truth for alembic runtime).
FIXED_PREV_OF = {
    "m4b000000001": "a1b2c3d4e5f0",
    "m4c000000001": "m4b000000001",
    "m4d000000001": "m4c000000001",
}


def _current_url():
    return settings.database_url


def _superuser_create_db(super_engine, new_db_name: str) -> bool:
    """Try CREATE DATABASE via superuser; return True if succeeded."""
    try:
        with super_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            safe = new_db_name.replace('"', '')
            conn.execute(text(f'DROP DATABASE IF EXISTS "{safe}"'))
            conn.execute(text(f'CREATE DATABASE "{safe}"'))
        return True
    except Exception:
        return False


def _replace_db_in_url(url: str, new_db: str) -> str:
    """Replace the path component (database name) inside a sqlalchemy URL."""
    from sqlalchemy.engine import make_url
    u = make_url(url)
    # set database to new_db by reconstructing
    new_url = u.set(database=new_db)
    return new_url.render_as_string(hide_password=False)


def _clean_schema_public(engine):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
        c.execute(text("GRANT ALL ON SCHEMA public TO public"))


def _run_alembic(args, env_extra=None, cwd=None):
    import subprocess
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    # Pass DATABASE_URL wins as alembic -x db_url=... so that env.py will call
    # config.set_main_option("sqlalchemy.url", ...) using that URL regardless of
    # settings.database_url default.
    extra_args = []
    new_url = env_extra.get("DATABASE_URL") if env_extra else None
    if new_url:
        extra_args = ["-x", f"db_url={new_url}"]
    cp = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *extra_args, *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        env=env,
    )
    return cp


def _alembic_current_rev(env_extra=None) -> str | None:
    cp = _run_alembic(["current"], env_extra=env_extra)
    out = (cp.stdout or b"").decode("utf-8", errors="replace")
    import re
    TOK = re.compile(r"[A-Za-z0-9_]{8,}")
    for ln in out.splitlines():
        m = TOK.search(ln)
        if m:
            return m.group(0)
    return None


def _alembic_heads_count(env_extra=None) -> int:
    cp = _run_alembic(["heads"], env_extra=env_extra)
    out = (cp.stdout or b"").decode("utf-8", errors="replace")
    return len([ln for ln in out.splitlines() if ln.strip()])


def _db_can_connect(engine) -> bool:
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def _step(name: str, target: str, direction: str, expected_rev: str | None,
          env_extra: dict, engine) -> dict:
    cp = _run_alembic([direction, target], env_extra=env_extra)
    actual = _alembic_current_rev(env_extra=env_extra)
    rc = cp.returncode
    err = None
    if rc != 0:
        err_tail = (cp.stderr or b"").decode("utf-8", errors="replace").strip()[-2000:]
        err = err_tail or (cp.stdout or b"").decode("utf-8", errors="replace")[-500:]
    ok = (
        rc == 0
        and (actual == expected_rev or (expected_rev is None and actual is None))
        and _db_can_connect(engine)
    )
    return {
        "step": name,
        "direction": direction,
        "target": target,
        "expected_rev": expected_rev,
        "actual_rev": actual,
        "db_connect_ok": _db_can_connect(engine),
        "returncode": rc,
        "error": err,
        "ok": ok,
    }


def main() -> int:
    out: dict = {}
    BASE_URL = _current_url()
    out["base_url_masked"] = BASE_URL.split("@")[-1] if "@" in BASE_URL else BASE_URL

    disposable_db_name = f"pasay_m005_rt_{os.getpid()}_{int(__import__('time').time() * 1000)}"

    # 1) Try to create a true throwaway DB. Fallback: reuse existing DB but
    # DROP SCHEMA public CASCADE for a clean slate (also disposable proof).
    disposable_mode = "createdb"
    super_engine = None
    run_url: str | None = None
    try:
        # Try with a superuser-style URL that might exist on dev machines;
        # fall back to constructing a new DB name on the existing server.
        from sqlalchemy.engine import make_url
        base_u = make_url(BASE_URL)
        # Try to use the same user/pass to create a new database.
        su_url = base_u.set(database="postgres")
        super_engine = create_engine(su_url.render_as_string(hide_password=False), pool_pre_ping=True)
        ok = _superuser_create_db(super_engine, disposable_db_name)
        if ok:
            run_url_obj = base_u.set(database=disposable_db_name)
            run_url = run_url_obj.render_as_string(hide_password=False)
            disposable_mode = "createdb"
    except Exception:
        pass

    if run_url is None:
        disposable_mode = "drop_schema_public"
        run_url = BASE_URL
        # Ensure schema public is clean for an equivalent fresh DB.
        try:
            base_engine = create_engine(BASE_URL, pool_pre_ping=True)
            _clean_schema_public(base_engine)
            base_engine.dispose()
        except Exception as e:
            out["fatal"] = f"cannot prepare disposable DB: {type(e).__name__}: {e}"
            sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            return 1

    out["disposable_mode"] = disposable_mode
    out["disposable_db_name"] = disposable_db_name if disposable_mode == "createdb" else f"<reused with DROP SCHEMA public>: {out['base_url_masked']}"

    engine = create_engine(run_url, pool_pre_ping=True)
    out["db_connect_initial"] = _db_can_connect(engine)
    if not out["db_connect_initial"]:
        out["fatal"] = "cannot connect to run_url"
        sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return 1

    ENV_EXTRA = {
        "DATABASE_URL": run_url,
        "DATABASE_URL_UNPOOLED": run_url,
    }
    if disposable_mode == "createdb":
        out["env_extra_overrode_DATABASE_URL"] = True
    # alembic uses alembic.ini sqlalchemy.url which is driven by config from
    # app.settings via env DATABASE_URL. If alembic.ini has a hardcoded URL we
    # also need to provide it via env for certain alembic setups; but our
    # alembic/env.py reads from settings which read env DATABASE_URL, so this
    # works.

    heads = _alembic_heads_count(env_extra=ENV_EXTRA)
    out["single_head_ok"] = heads == 1
    if not out["single_head_ok"]:
        out["fatal"] = f"heads count = {heads} != 1"
        sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return 1

    # Upgrade head first to set baseline; prove we can start from the newest.
    cp_up_head = _run_alembic(["upgrade", "head"], env_extra=ENV_EXTRA)
    out["upgrade_head_initial_rc"] = cp_up_head.returncode
    if cp_up_head.returncode != 0:
        out["upgrade_head_initial_stderr"] = (cp_up_head.stderr or b"").decode("utf-8", errors="replace")[-2000:]
        out["fatal"] = "cannot upgrade head on fresh disposable DB"
        sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return 1
    cur = _alembic_current_rev(env_extra=ENV_EXTRA)
    out["initial_head_rev"] = cur

    # Make a plan:
    # For each rev in FINAL_3_REV_ORDER [m4b, m4c, m4d (head)]:
    #   1) ensure current = prev (m4a, m4b, m4c respectively) via downgrade if needed
    #   2) upgrade -> rev  (step 1)
    #   3) downgrade -> prev (step 2)
    #   4) upgrade -> rev (step 3)
    # After loop: leave DB at HEAD (m4d) overall.
    # Determine prev revisions: use alembic downgrade -N semantics (m4b's prev is m4a, m4c's prev is m4b, m4d's prev is m4c).
    # - For m4b, prev = "base-1" meaning "first migration before m4b". Alembic supports: downgrade to target m4a rev id; but we don't know m4a.
    #   Alternative: use `-1` from position. To simplify: we'll step through by using explicit rev targets of prior revisions.
    #   First find all revisions order and their "prev".

    # Get full ordered list.
    all_revs_cp = _run_alembic(["history", "-v"], env_extra=ENV_EXTRA)
    all_revs_out = (all_revs_cp.stdout or b"").decode("utf-8", errors="replace")
    import re
    ARROW = re.compile(r"([0-9A-Za-z_]{8,})\s*->\s*([0-9A-Za-z_]{8,})")
    rev_order = []
    for m in ARROW.finditer(all_revs_out):
        for tok in (m.group(1), m.group(2)):
            if len(tok) < 8 or not re.fullmatch(r"[0-9a-fA-F]{8,}|m\d+[A-Za-z0-9_]*", tok):
                continue
            if tok not in rev_order:
                rev_order.append(tok)
    out["all_revisions_count_parsed"] = len(rev_order)

    # Build prev map (reverse arrows).
    prev_of = {}
    for m in ARROW.finditer(all_revs_out):
        a, b = m.group(1), m.group(2)
        if re.fullmatch(r"[0-9a-fA-F]{8,}|m\d+[A-Za-z0-9_]*", a) and re.fullmatch(r"[0-9a-fA-F]{8,}|m\d+[A-Za-z0-9_]*", b):
            prev_of[b] = a

    final3_in_reality = list(FINAL_3_REV_ORDER)
    out["final3_revisions"] = final3_in_reality
    out["final3_prevs"] = {r: FIXED_PREV_OF.get(r) for r in final3_in_reality}

    results_per_rev: list[dict] = []
    all_steps_ok = True

    # Start by downgrading to the rev *before* the first target.
    first_rev = final3_in_reality[0]
    first_prev = out["final3_prevs"].get(first_rev)
    prep_steps = []

    if first_prev:
        s = _step("prep_downgrade_before_first", first_prev, "downgrade", first_prev, ENV_EXTRA, engine)
        prep_steps.append(s)
        if not s["ok"]:
            # Debug capture: run downgrade a second time with stderr streamed to
            # JSON so we can see the actual alembic exception (not just rc=0
            # with actual=None).
            cp2 = _run_alembic(["downgrade", first_prev], env_extra=ENV_EXTRA)
            out["prep_debug"] = {
                "downgrade_rc": cp2.returncode,
                "downgrade_stdout": (cp2.stdout or b"").decode("utf-8", errors="replace")[-3000:],
                "downgrade_stderr": (cp2.stderr or b"").decode("utf-8", errors="replace")[-4000:],
                "alembic_current_stdout": None,
            }
            cp3 = _run_alembic(["current"], env_extra=ENV_EXTRA)
            out["prep_debug"]["alembic_current_stdout"] = (cp3.stdout or b"").decode("utf-8", errors="replace")
            out["fatal_prep"] = "cannot downgrade to rev before first final-3"
            out["prep_steps"] = prep_steps
            engine.dispose()
            if super_engine and disposable_mode == "createdb":
                try:
                    with super_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
                        c.execute(text(f'DROP DATABASE IF EXISTS "{disposable_db_name}"'))
                except Exception:
                    pass
            sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            return 1

    # Now iterate final 3 (m4b, m4c, m4d=head):
    for idx, rev in enumerate(final3_in_reality):
        prev = out["final3_prevs"].get(rev)
        next_rev = final3_in_reality[idx + 1] if idx + 1 < len(final3_in_reality) else None
        # Position check: current should be prev (or None if first)
        steps_rev: list[dict] = []
        # Step 1: upgrade -> rev
        steps_rev.append(_step(f"{rev}__upgrade", rev, "upgrade", rev, ENV_EXTRA, engine))
        # Step 2: downgrade -> prev
        steps_rev.append(_step(f"{rev}__downgrade_back", "-1" if prev is None else prev,
                               "downgrade", prev, ENV_EXTRA, engine))
        # Step 3: upgrade -> rev
        steps_rev.append(_step(f"{rev}__upgrade_again", rev, "upgrade", rev, ENV_EXTRA, engine))
        rev_ok = all(s["ok"] for s in steps_rev)
        results_per_rev.append({
            "revision": rev,
            "previous_revision": prev,
            "next_revision": next_rev,
            "all_steps_ok": rev_ok,
            "steps": steps_rev,
        })
        if not rev_ok:
            all_steps_ok = False

    # Restore head at the end.
    restore_step = _step("restore_final_head", "head", "upgrade", final3_in_reality[-1], ENV_EXTRA, engine)
    out["restore_final_head_step"] = restore_step
    out["restored_head_rev"] = _alembic_current_rev(env_extra=ENV_EXTRA)

    out["prep_steps"] = prep_steps
    out["roundtrip_results"] = results_per_rev
    out["overall_roundtrip_ok"] = all_steps_ok and restore_step["ok"] and out["single_head_ok"]

    engine.dispose()
    if super_engine and disposable_mode == "createdb":
        try:
            with super_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
                c.execute(text(f'DROP DATABASE IF EXISTS "{disposable_db_name}"'))
            out["disposable_db_cleanup_dropped"] = True
        except Exception as e:
            out["disposable_db_cleanup_error"] = f"{type(e).__name__}: {e}"

    sys.stdout.buffer.write((json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0 if out["overall_roundtrip_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
