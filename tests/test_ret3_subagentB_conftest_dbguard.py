"""Regression test for tests/conftest.py DB-name guard (Issue #RET3-SUBAGENT-B).

Verifies that the test-database name guard:
  1. REFUSES to run when PASAY_TEST_DB_NAME would target a production-like
     database (fullmatch check — a name that merely *contains* "pasay" as a
     substring such as "production_pasay" must be rejected, not accepted).
  2. ALLOWS a real isolated test database name that matches the approved
     full pattern family (e.g. "test_pasay_xyz" or "pasay_pm_r1_001").
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFTEST_PATH = PROJECT_ROOT / "tests" / "conftest.py"


def _run_conftest_guard_env(env_overrides: dict) -> tuple[int, str, str]:
    """Spawn a fresh Python interpreter that imports the guard portion of
    tests/conftest.py with the given env overrides. Returns (rc, stdout, stderr).

    The guard lives at module top-level in tests/conftest.py and raises
    SystemExit when PASAY_TEST_DB_NAME is not allowed. We isolate it in a
    subprocess because importing conftest.py directly in this test process
    would fire the guard *once* during collection with our real env, and we
    want to exercise multiple different PASAY_TEST_DB_NAME values without
    reloading trickery."""
    env = os.environ.copy()
    env.pop("PASAY_TEST_DB_NAME", None)
    env.pop("DATABASE_URL", None)
    env.update(env_overrides)
    code = (
        "import importlib.util, sys, os\n"
        "spec = importlib.util.spec_from_file_location("
        "'_conftest_guard', r'%s')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        # We cannot exec the *entire* conftest.py unconditionally because it
        # pulls in app.main / SQLA / etc. which need a real DB driver. Instead
        # we execute ONLY the guard prefix (lines 1..~75) — the portion that
        # defines _ALLOWED_TEST_DB_PREFIX_RE / _test_db_allowed() and raises
        # SystemExit. We read the source and exec until right before
        # test_engine() is defined.
        "src = open(spec.origin, encoding='utf-8').read()\n"
        "cut = src.index('@pytest.fixture(scope=\"session\")')\n"
        "exec(compile(src[:cut], spec.origin, 'exec'), mod.__dict__)\n"
        "print('GUARD_OK', mod._test_db_allowed("
        "os.getenv('PASAY_TEST_DB_NAME', ''), "
        "os.getenv('_CFG_DB', 'postgres_live')))\n"
    ) % str(CONFTEST_PATH).replace("\\", "\\\\")
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    return proc.returncode, proc.stdout, proc.stderr


class TestDbGuardFullMatch:
    def test_production_pasay_refused(self):
        """'production_pasay' only *contains* the token 'pasay' as a substring
        — it is not an approved test-database full name. The guard MUST raise
        SystemExit / refuse. This is the core regression: a substring-based
        check would falsely accept this name and let pytest drop real tables."""
        rc, out, err = _run_conftest_guard_env({
            "PASAY_TEST_DB_NAME": "production_pasay",
            "DATABASE_URL": "postgresql+psycopg2://u:p@h:5432/postgres_live",
            "_CFG_DB": "postgres_live",
        })
        assert rc != 0, (
            f"Expected guard to REFUSE 'production_pasay' and exit non-zero, "
            f"got rc=0.\nSTDOUT={out!r}\nSTDERR={err!r}"
        )
        combined = out + "\n" + err
        assert "REFUSED" in combined or "SystemExit" in combined or rc == 1, (
            f"Guard refusal for 'production_pasay' must mention REFUSED or "
            f"exit via SystemExit. Got rc={rc}, output={combined!r}"
        )
        assert "GUARD_OK" not in out, (
            "Guard must NOT print GUARD_OK when name is refused "
            "(i.e. SystemExit fires before the print)."
        )

    def test_test_pasay_xyz_allowed(self):
        """'test_pasay_xyz' matches the approved full pattern
        ``test_pasay_[a-zA-Z0-9_]+`` — it is a real isolated test DB name.
        Guard must accept it (no SystemExit) and _test_db_allowed == True."""
        rc, out, err = _run_conftest_guard_env({
            "PASAY_TEST_DB_NAME": "test_pasay_xyz",
            "DATABASE_URL": "postgresql+psycopg2://u:p@h:5432/postgres_live",
            "_CFG_DB": "postgres_live",
        })
        assert rc == 0, (
            f"Expected guard to ALLOW 'test_pasay_xyz' (rc=0), got rc={rc}.\n"
            f"STDOUT={out!r}\nSTDERR={err!r}"
        )
        assert "GUARD_OK True" in out, (
            f"_test_db_allowed('test_pasay_xyz', …) must return True. "
            f"STDOUT={out!r}"
        )

    def test_pasay_pm_without_family_suffix_refused(self):
        """Legacy bare 'pasay_pm_test' must be refused — the approved families
        require an extra suffix after the family token (e.g. pasay_pm_r1_001
        not pasay_pm_test). We also refuse the exact forbidden literal names
        to ensure fail-closed on the hardcoded production name list."""
        rc, out, err = _run_conftest_guard_env({
            "PASAY_TEST_DB_NAME": "pasay_pm",
            "DATABASE_URL": "postgresql+psycopg2://u:p@h:5432/postgres_live",
            "_CFG_DB": "postgres_live",
        })
        assert rc != 0, (
            f"Expected guard to REFUSE literal 'pasay_pm' (forbidden list), "
            f"got rc=0.\nSTDERR={err!r}"
        )

    def test_approved_pasay_pm_style_allowed(self):
        """Names matching 'pasay_pm_<family-suffix>_<tail>' (the original
        strict whitelist) must continue to be allowed so existing CI
        pipelines keep working (e.g. PASAY_TEST_DB_NAME=pasay_pm_test_xyz)."""
        rc, out, err = _run_conftest_guard_env({
            "PASAY_TEST_DB_NAME": "pasay_pm_test_xyz",
            "DATABASE_URL": "postgresql+psycopg2://u:p@h:5432/postgres_live",
            "_CFG_DB": "postgres_live",
        })
        assert rc == 0, (
            f"Expected guard to ALLOW 'pasay_pm_test_xyz' (rc=0), "
            f"got rc={rc}.\nSTDERR={err!r}"
        )
        assert "GUARD_OK True" in out

    def test_same_as_configured_live_db_refused(self):
        """Even if the name *would* match the test pattern, when it equals
        the configured live DATABASE_URL database the guard must still
        refuse — prevents a misconfigured override accidentally pointing at
        production."""
        rc, out, err = _run_conftest_guard_env({
            "PASAY_TEST_DB_NAME": "test_pasay_xyz",
            "DATABASE_URL": "postgresql+psycopg2://u:p@h:5432/test_pasay_xyz",
            "_CFG_DB": "test_pasay_xyz",
        })
        assert rc != 0, (
            "Expected guard to REFUSE when PASAY_TEST_DB_NAME equals the "
            "configured live DB name, even if it looks like a test pattern. "
            f"Got rc=0.\nSTDERR={err!r}"
        )
