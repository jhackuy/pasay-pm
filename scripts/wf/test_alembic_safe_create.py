"""PASAY Alembic Safe Migration Creation Helper — targeted tests (Issue #65).

Tests the two scenarios from Issue #65 Required Tests that apply to the helper:

    7. safe creation helper reads real current head
    8. helper on multiple heads fails closed
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "scripts" / "wf" / "alembic_fixtures"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "wf"))
from alembic_safe_create import (  # noqa: E402
    SafeCreateError,
    safe_create_revision,
    _read_current_heads,
    _resolve_predecessor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _copy_fixture_to_tmp(name: str) -> Path:
    """Copy a fixture directory to a temp location so we can mutate it."""
    import tempfile
    src = FIXTURES / name
    dst = Path(tempfile.mkdtemp(prefix=f"alembic_safe_create_{name}_"))
    shutil.copytree(src, dst / "alembic_fx")
    return dst / "alembic_fx"


# ---------------------------------------------------------------------------
# 7. safe creation helper reads real current head
# ---------------------------------------------------------------------------


def test_07_safe_create_reads_real_head_in_single_head_graph():
    """Helper must read the real current head and use it as the predecessor."""
    fx = _copy_fixture_to_tmp("valid_single_head")
    try:
        # Pre-condition: exactly one head
        heads = _read_current_heads(fx)
        assert heads == ["a00000000001"], heads

        res = safe_create_revision(fx, message="test_safe_create_07")
        assert res.ok, res.error
        assert res.predecessor == "a00000000001"
        assert res.revision and res.revision != "a00000000001"
        assert res.path and res.path.exists()
        # Verify the new file is well-formed: down_revision points to the real head
        from alembic_graph_gate import parse_migration_file
        info = parse_migration_file(res.path)
        assert info["revision"] == res.revision
        assert info["down_revision"] == "a00000000001"
    finally:
        shutil.rmtree(fx.parent, ignore_errors=True)


def test_07b_safe_create_writes_into_versions_dir():
    """The new file must be created in the resolved versions/ directory."""
    fx = _copy_fixture_to_tmp("valid_single_head")
    try:
        res = safe_create_revision(fx, message="in_versions")
        assert res.ok, res.error
        assert res.path.parent.name == "versions"
        assert res.path.parent.is_dir()
    finally:
        shutil.rmtree(fx.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8. helper on multiple heads fails closed
# ---------------------------------------------------------------------------


def test_08_safe_create_multi_head_fails_closed_without_explicit_head():
    """If the graph has multiple heads and the caller does not specify --head,
    the helper must fail closed (refuse to create)."""
    fx = _copy_fixture_to_tmp("multiple_heads_unintended")
    try:
        heads = _read_current_heads(fx)
        assert sorted(heads) == ["u00000000001", "u00000000002"], heads

        res = safe_create_revision(fx, message="test_safe_create_08")
        assert not res.ok, "helper must fail closed on multi-head without --head"
        assert "multiple" in res.error.lower() or "heads" in res.error.lower(), res.error
        assert sorted(res.heads_at_create) == sorted(heads)
    finally:
        shutil.rmtree(fx.parent, ignore_errors=True)


def test_08b_safe_create_multi_head_succeeds_with_explicit_head():
    """When --head is given and matches a current head, the helper succeeds
    and uses that head as the predecessor."""
    fx = _copy_fixture_to_tmp("multiple_heads_unintended")
    try:
        res = safe_create_revision(fx, message="branch_a_extension", head="u00000000001")
        assert res.ok, res.error
        assert res.predecessor == "u00000000001"
        from alembic_graph_gate import parse_migration_file
        info = parse_migration_file(res.path)
        assert info["down_revision"] == "u00000000001"
    finally:
        shutil.rmtree(fx.parent, ignore_errors=True)


def test_08c_safe_create_rejects_invalid_explicit_head():
    """If --head is given but is not a current head, helper refuses."""
    fx = _copy_fixture_to_tmp("multiple_heads_unintended")
    try:
        res = safe_create_revision(fx, message="bogus_head", head="nonexistent")
        assert not res.ok, "helper must reject --head not in current heads"
        assert "not among" in res.error or "not in" in res.error, res.error
    finally:
        shutil.rmtree(fx.parent, ignore_errors=True)


def test_08d_safe_create_rejects_explicit_head_mismatch_in_single_head_graph():
    """In a single-head graph, passing an --head that doesn't match the unique
    head must fail closed — refuse to silently create a diverging branch."""
    fx = _copy_fixture_to_tmp("valid_single_head")
    try:
        res = safe_create_revision(fx, message="diverging", head="b00000000999")
        assert not res.ok
        assert "diverg" in res.error.lower() or "does not match" in res.error.lower(), res.error
    finally:
        shutil.rmtree(fx.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# Bonus: helper refuses to create when the graph is broken
# ---------------------------------------------------------------------------


def test_safe_create_fails_when_graph_dangling():
    """If the existing graph has a dangling down_revision (regression case),
    the helper must refuse — it MUST NOT silently create a sibling revision
    that pretends the graph is healthy."""
    fx = _copy_fixture_to_tmp("filename_as_down_revision")
    try:
        res = safe_create_revision(fx, message="should_not_create")
        assert not res.ok, "helper must fail closed on broken graph"
        assert "graph" in res.error.lower() or "keyerror" in res.error.lower() or "scriptdirectory" in res.error.lower(), res.error
        # No file should have been created
        versions_dir = fx / "versions"
        new_files = [p for p in versions_dir.iterdir() if "should_not_create" in p.name]
        assert not new_files, f"unexpectedly created file(s): {new_files}"
    finally:
        shutil.rmtree(fx.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# _resolve_predecessor unit tests
# ---------------------------------------------------------------------------


def test_resolve_predecessor_no_heads():
    assert _resolve_predecessor([], None) is None


def test_resolve_predecessor_single_head_no_explicit():
    assert _resolve_predecessor(["abc"], None) == "abc"


def test_resolve_predecessor_single_head_matching_explicit():
    assert _resolve_predecessor(["abc"], "abc") == "abc"


def test_resolve_predecessor_single_head_mismatching_explicit():
    with pytest.raises(SafeCreateError):
        _resolve_predecessor(["abc"], "xyz")


def test_resolve_predecessor_multi_head_requires_explicit():
    with pytest.raises(SafeCreateError):
        _resolve_predecessor(["a", "b"], None)


def test_resolve_predecessor_multi_head_invalid_explicit():
    with pytest.raises(SafeCreateError):
        _resolve_predecessor(["a", "b"], "c")


def test_resolve_predecessor_multi_head_valid_explicit():
    assert _resolve_predecessor(["a", "b"], "b") == "b"
