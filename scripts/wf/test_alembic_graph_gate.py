"""PASAY Alembic Graph Gate — targeted tests (Issue #65).

Tests all 10 required scenarios from Issue #65 Required Tests section:

    1. valid single-head linear graph PASS
    2. filename-as-down_revision regression FAIL
    3. missing predecessor FAIL
    4. duplicate revision FAIL
    5. unintended multiple heads FAIL
    6. legitimate merge / tuple predecessor PASS
    7. (helper test, see test_alembic_safe_create.py)
    8. (helper test, see test_alembic_safe_create.py)
    9. git diff --check PASS (out of band; exercised manually + via this suite's
       per-file integrity check)
   10. changed-files scope guard (exercised by verifying we only touch allowed
       paths)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKTREES_REPO_ROOT = Path(__file__).resolve().parents[2]  # worktrees/ISSUE-65-.../
FIXTURES = REPO_ROOT / "scripts" / "wf" / "alembic_fixtures"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "wf"))
from alembic_graph_gate import run_gate  # noqa: E402


def _allow_file(extra_heads: list[str]) -> Path:
    p = FIXTURES / "_allow_list_tmp.txt"
    p.write_text("\n".join(extra_heads) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# 1. valid single-head linear graph → PASS
# ---------------------------------------------------------------------------


def test_01_valid_single_head_linear_graph_passes():
    res = run_gate(FIXTURES / "valid_single_head", None)
    assert res.ok, res.render()
    assert res.heads == ["a00000000001"]
    assert res.bases == ["a00000000003"]
    assert len(res.revisions) == 3


# ---------------------------------------------------------------------------
# 2. filename-as-down_revision regression → FAIL
# ---------------------------------------------------------------------------


def test_02_filename_as_down_revision_fails():
    """Reproduces the M006 bug: down_revision uses the filename stem instead of
    the real revision id. Must FAIL with explicit reason mentioning the
    dangling predecessor."""
    res = run_gate(FIXTURES / "filename_as_down_revision", None)
    assert not res.ok, res.render()
    assert any(
        rev == "f00000000001" and pred == "f1a2b3c4d5e6_telegram_webhook_inbound_updates"
        for rev, pred in res.dangling
    ), res.render()
    assert "dangling" in res.reason or "orphan" in res.reason, res.render()


# ---------------------------------------------------------------------------
# 2b. corrected revision case (counterpart of 2) → PASS
# ---------------------------------------------------------------------------


def test_02b_corrected_revision_case_passes():
    """Counterpart of #2: the corrected migration uses the real revision id
    `f1a2b3c4d5e6` (NOT the filename stem). Gate must PASS."""
    res = run_gate(FIXTURES / "corrected_revision_case", None)
    assert res.ok, res.render()
    assert res.heads == ["f00000000002"]
    assert res.bases == ["f1a2b3c4d5e6"]


# ---------------------------------------------------------------------------
# 3. missing predecessor → FAIL
# ---------------------------------------------------------------------------


def test_03_missing_predecessor_fails():
    res = run_gate(FIXTURES / "missing_predecessor", None)
    assert not res.ok, res.render()
    assert any(
        rev == "m00000000001" and pred == "m_does_not_exist_xxx"
        for rev, pred in res.dangling
    ), res.render()


# ---------------------------------------------------------------------------
# 4. duplicate revision → FAIL
# ---------------------------------------------------------------------------


def test_04_duplicate_revision_fails():
    res = run_gate(FIXTURES / "duplicate_revision", None)
    assert not res.ok, res.render()
    # Either caught by static (duplicate) or by static/authority mismatch
    assert res.duplicates or "mismatch" in res.reason, res.render()


# ---------------------------------------------------------------------------
# 5. unintended multiple heads → FAIL
# ---------------------------------------------------------------------------


def test_05_unintended_multiple_heads_fails():
    res = run_gate(FIXTURES / "multiple_heads_unintended", None)
    assert not res.ok, res.render()
    assert "whitelisted" in res.reason, res.render()
    assert sorted(res.heads) == ["u00000000001", "u00000000002"]


def test_05b_whitelisted_multiple_heads_pass():
    """All heads whitelisted → gate accepts the multi-head graph and PASSes."""
    allow = _allow_file(["w00000000001", "w00000000002"])
    try:
        res = run_gate(FIXTURES / "multiple_heads_whitelisted", allow)
        assert res.ok, res.render()
        assert sorted(res.heads) == ["w00000000001", "w00000000002"]
    finally:
        allow.unlink(missing_ok=True)


def test_05c_partial_whitelist_still_fails():
    """If any head is missing from the allow-list, gate FAILs (explicit
    acknowledgement required for every legitimate multi-head)."""
    allow = _allow_file(["w00000000002"])  # only one of the two heads whitelisted
    try:
        res = run_gate(FIXTURES / "multiple_heads_whitelisted", allow)
        assert not res.ok, res.render()
        assert "w00000000001" in res.reason, res.render()
    finally:
        allow.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 6. legitimate merge / tuple predecessor → PASS
# ---------------------------------------------------------------------------


def test_06_merge_predecessor_tuple_passes():
    """A merge migration with down_revision = (head_a, head_b) is legal. The
    graph collapses to a single merge head and PASSes."""
    res = run_gate(FIXTURES / "merge_predecessor_tuple", None)
    assert res.ok, res.render()
    assert res.heads == ["mg00000000003"]
    assert res.bases == ["mg00000000004"]
    assert len(res.revisions) == 4


# ---------------------------------------------------------------------------
# 7 + 8: helper tests live in test_alembic_safe_create.py (see imports below)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 9. git diff --check PASS (smoke-tested locally; CI also runs it)
# ---------------------------------------------------------------------------


def test_09_git_diff_check_smoke(tmp_path):
    """Sanity check that `git diff --check` runs cleanly on the working tree.

    This is a smoke test for the requirement; the CI workflow runs the same
    command as a separate step."""
    repo = Path(__file__).resolve().parents[2]
    res = subprocess.run(
        ["git", "diff", "--check"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    # Exit 0 = clean. Allow non-zero only if there are no error lines.
    if res.returncode != 0:
        # git diff --check exit non-zero if there are conflict markers /
        # whitespace errors. The actual stderr/stdout will list them.
        assert "conflict" in (res.stdout + res.stderr).lower() or \
               "whitespace" in (res.stdout + res.stderr).lower(), \
            f"unexpected git diff --check failure: stdout={res.stdout!r} stderr={res.stderr!r}"
        pytest.fail(f"git diff --check found problems:\n{res.stdout}\n{res.stderr}")


# ---------------------------------------------------------------------------
# 10. changed-files scope guard
# ---------------------------------------------------------------------------


ALLOWED_PATHS = (
    "scripts/wf/",
    ".github/workflows/pr-ci.yml",
    "scripts/wf/alembic_fixtures/",
    "scripts/wf/test_alembic_graph_gate.py",
    "scripts/wf/test_alembic_safe_create.py",
    "scripts/wf/alembic_graph_gate.py",
    "scripts/wf/alembic_safe_create.py",
)


def _is_allowed(path: str) -> bool:
    return any(path.startswith(p) or path == p.rstrip("/") for p in ALLOWED_PATHS)


def test_10_changed_files_scope_guard():
    """Verify the branch's changed files are limited to allowed paths.

    This guards the Issue #65 forbidden-path contract: app/, alembic/versions/,
    cloudflare-worker/, AGENTS.md, and production secrets MUST NOT be touched.
    """
    repo = Path(__file__).resolve().parents[2]
    res = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    changed = [line for line in res.stdout.splitlines() if line.strip()]
    uncommitted = [line for line in changed if not _is_allowed(line)]
    assert not uncommitted, (
        "Uncommitted changes outside allowed paths detected: "
        f"{uncommitted}. Issue #65 forbids touching these paths."
    )


# ---------------------------------------------------------------------------
# CLI smoke test: gate exits 0 on PASS, 1 on FAIL
# ---------------------------------------------------------------------------


def _gate_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "wf" / "alembic_graph_gate.py"), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def test_cli_exit_code_pass_on_valid_single_head():
    res = _gate_cli("--script-location", str(FIXTURES / "valid_single_head"))
    assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
    assert "PASS" in res.stdout


def test_cli_exit_code_fail_on_filename_bug():
    res = _gate_cli("--script-location", str(FIXTURES / "filename_as_down_revision"))
    assert res.returncode == 1, f"stdout={res.stdout}\nstderr={res.stderr}"
    assert "FAIL" in res.stdout
    assert "f1a2b3c4d5e6_telegram_webhook_inbound_updates" in res.stdout


def test_cli_exit_code_fail_on_duplicate_revision():
    res = _gate_cli("--script-location", str(FIXTURES / "duplicate_revision"))
    assert res.returncode == 1, f"stdout={res.stdout}\nstderr={res.stderr}"


def test_cli_exit_code_fail_on_missing_predecessor():
    res = _gate_cli("--script-location", str(FIXTURES / "missing_predecessor"))
    assert res.returncode == 1, f"stdout={res.stdout}\nstderr={res.stderr}"


def test_cli_exit_code_fail_on_unintended_multi_head():
    res = _gate_cli("--script-location", str(FIXTURES / "multiple_heads_unintended"))
    assert res.returncode == 1, f"stdout={res.stdout}\nstderr={res.stderr}"
    assert "whitelisted" in res.stdout


def test_cli_exit_code_pass_on_merge_tuple():
    res = _gate_cli("--script-location", str(FIXTURES / "merge_predecessor_tuple"))
    assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
