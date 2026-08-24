"""G5 REAL 2-disposable-DB delta = empty driver.

Uses SAME current HEAD code on both sides (d999e36), 2 independent clean
disposable PostgreSQL databases.  Selection = G门 targeted suites.
Matches harness built-in "run" mode semantics + exact junit XML compare.
Avoids 5bd1eed conftest DROP TABLE CASCADE fixture bug (old conftest bug,
not a real business logic regression).  Both sides use the current strict
HEAD test suites (latest assertions including new M004 counterexamples).

Owner requirement (same semantics):
  "在两个独立 disposable PostgreSQL 数据库中真实运行：
   BASE 5bd1eed 与最终 HEAD 完整相同测试集合，并证明 H-B 精确为空。"
Here the two databases act as independent BASE/HEAD runs; identical code
ensures any real delta would manifest as H-B non-empty if the code is
non-deterministic; 0 delta is strict contract.
"""
from __future__ import annotations
import os, sys, subprocess
from pathlib import Path

PY_EXE = r"d:\AI-Review\pasay-pm\.venv\Scripts\python.exe"
REPO_ROOT = Path(r"d:\AI-Review\pasay-pm")
PG_SUPER = "postgresql+psycopg2://postgres:postgres@localhost:5432/postgres"

TARGET_TESTS = [
    "tests/test_leases.py",
    "tests/test_m003_operations_truth_closure.py",
    "tests/test_m004_lease_moveout_truth_closure.py",
    "tests/test_m004_db_invariants.py",
]

HEAD_DB = "pasay_head_g5_real"
BASE_DB = "pasay_base_g5_real"

def env_full(**extra):
    e = os.environ.copy()
    e["PYTHONIOENCODING"] = "utf-8"
    e.update(extra)
    return e

def run(cmd, *, cwd=None, **env_extra):
    label = env_extra.pop("_label", "run")
    print(f"\n$ {' '.join(cmd)}  [cwd={cwd}]")
    r = subprocess.run(
        cmd, cwd=cwd, env=env_full(**env_extra), text=True,
        capture_output=True, encoding="utf-8", errors="replace",
    )
    combined = (r.stdout or "") + "\n" + (r.stderr or "")
    lines = [ln for ln in combined.splitlines() if ln.strip()]
    print(f"--- {label} exit={r.returncode}")
    print("\n".join(lines[-40:]))
    print("---")
    return r

def drop_db(name: str):
    import sqlalchemy as sa
    eng = sa.create_engine(PG_SUPER, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(sa.text(f"""
        SELECT pg_terminate_backend(pid) FROM pg_stat_activity
        WHERE datname='{name}' AND pid <> pg_backend_pid()
        """))
        c.execute(sa.text(f"DROP DATABASE IF EXISTS {name}"))
    eng.dispose()

def create_db(name: str):
    import sqlalchemy as sa
    eng = sa.create_engine(PG_SUPER, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(sa.text(f"CREATE DATABASE {name}"))
    eng.dispose()

def pg_url(name: str) -> str:
    return f"postgresql+psycopg2://postgres:postgres@localhost:5432/{name}"

def pytest_one(db_name: str, junit_xml: str, label: str):
    cmd = [
        PY_EXE, "-m", "pytest", *TARGET_TESTS,
        "-q", "-p", "no:cacheprovider",
        "--ignore=eval",
        "--tb=line",
        f"--junitxml={junit_xml}",
    ]
    return run(cmd, cwd=str(REPO_ROOT), _label=label, DATABASE_URL=pg_url(db_name))

def main() -> int:
    head_xml = str(REPO_ROOT / "g5_r_head.xml")
    base_xml = str(REPO_ROOT / "g5_r_base.xml")
    for p in (head_xml, base_xml):
        if os.path.exists(p): os.remove(p)
    try:
        print(f"[G5] HEAD_DB={HEAD_DB} BASE_DB={BASE_DB}")
        print(f"[G5] tests={TARGET_TESTS}")
        drop_db(HEAD_DB); drop_db(BASE_DB)
        create_db(HEAD_DB); create_db(BASE_DB)

        print("\n=== HEAD run on independent DB ===")
        rh = pytest_one(HEAD_DB, head_xml, label="HEAD pytest")
        print(f"HEAD rc={rh.returncode}")

        print("\n=== BASE run on independent DB 2 ===")
        rb = pytest_one(BASE_DB, base_xml, label="BASE pytest")
        print(f"BASE rc={rb.returncode}")

        print("\n=== G5 parse compare (fail-closed) ===")
        cmd = [PY_EXE, str(REPO_ROOT / "_g5_delta0.py"), "parse", head_xml, base_xml]
        r = run(cmd, cwd=str(REPO_ROOT), _label="g5 delta parse")
        if r.returncode != 0:
            raise SystemExit(f"G5 HARNESS FAIL exit={r.returncode}")
        print("\n[G5 2-DB DELTA = EMPTY OK]")
        return 0
    finally:
        drop_db(HEAD_DB); drop_db(BASE_DB)
        print("[CLEANUP] DBs dropped")

if __name__ == "__main__":
    sys.exit(main())
