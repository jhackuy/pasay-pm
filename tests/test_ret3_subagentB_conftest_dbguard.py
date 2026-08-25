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
    src_text = CONFTEST_PATH.read_text(encoding="utf-8")
    marker = '@pytest.fixture(scope="session")'
    try:
        cut = src_text.index(marker)
    except ValueError as exc:
        raise AssertionError(
            f"Cannot locate source cut marker {marker!r} in conftest.py; "
            "harness must be updated to match conftest structure."
        ) from exc
    conftest_path_escaped = str(CONFTEST_PATH).replace("\\", "\\\\")
    guard_src_repr = repr(src_text[:cut])
    code = (
        "import importlib.util, sys, os\n"
        "GUARD_EXIT_MARKER = 'RETP_TEST_GUARD_RAN_TO_END'\n"
        "spec = importlib.util.spec_from_file_location(\n"
        f"    '_conftest_guard', r'{conftest_path_escaped}')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        f"src = {guard_src_repr}\n"
        "exec(compile(src, spec.origin, 'exec'), mod.__dict__)\n"
        "try:\n"
        "    result = mod._test_db_allowed(\n"
        "        os.getenv('PASAY_TEST_DB_NAME', ''),\n"
        "        os.getenv('_CFG_DB', 'postgres_live'))\n"
        "    print('GUARD_OK', result)\n"
        "except SystemExit as exc:\n"
        "    msg = exc.code if isinstance(exc.code, str) else str(exc.code)\n"
        "    print('GUARD_SYSEXIT', msg, file=sys.stderr)\n"
        "    raise\n"
        "print(GUARD_EXIT_MARKER)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    return proc.returncode, proc.stdout, proc.stderr


_CUT_NOT_FOUND_EXIT = 97


class TestDbGuardFullMatch:
    def _assert_refusal(self, rc: int, out: str, err: str, *, name_hint: str):
        combined = out + "\n" + err
        assert rc != 0 and rc != _CUT_NOT_FOUND_EXIT, (
            f"Expected guard to REFUSE {name_hint!r} with a real non-zero exit, "
            f"got rc={rc}. Harness bug if rc={_CUT_NOT_FOUND_EXIT}.\n"
            f"STDOUT={out!r}\nSTDERR={err!r}"
        )
        assert "GUARD_OK" not in out, (
            f"Guard must NOT print GUARD_OK when {name_hint!r} is refused "
            "(i.e. SystemExit or ValueError must fire before the result print)."
        )
        has_refusal_text = (
            "REFUSED" in combined
            or "SystemExit" in combined
            or "GUARD_SYSEXIT" in err
            or "not in allowed" in combined
            or "fail-closed guard" in combined
            or "ValueError" in combined
        )
        assert has_refusal_text, (
            f"Refusal for {name_hint!r} must produce diagnostic text mentioning "
            f"REFUSED / SystemExit / GUARD_SYSEXIT / the guard error. "
            f"rc={rc}, output={combined!r}"
        )

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
        self._assert_refusal(rc, out, err, name_hint="production_pasay")

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
        assert rc != _CUT_NOT_FOUND_EXIT, f"Harness bug: cut marker missing, rc={_CUT_NOT_FOUND_EXIT}"
        assert "GUARD_OK True" in out, (
            f"_test_db_allowed('test_pasay_xyz', …) must return True. "
            f"STDOUT={out!r}"
        )
        assert "RETP_TEST_GUARD_RAN_TO_END" in out, (
            "Harness must reach the end-of-code sentinel so we know the result "
            f"wasn't produced by a broken harness. STDOUT={out!r}"
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
        self._assert_refusal(rc, out, err, name_hint="pasay_pm")

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
        assert rc != _CUT_NOT_FOUND_EXIT
        assert "GUARD_OK True" in out
        assert "RETP_TEST_GUARD_RAN_TO_END" in out

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
        self._assert_refusal(rc, out, err, name_hint="same-as-live test_pasay_xyz")
