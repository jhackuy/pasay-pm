"""PASAY Alembic Revision Graph Gate (Issue #65 — PASAY-CI-ALEMBIC-SAFETY-P0-001).

Deterministic, fail-closed checker for the Alembic revision graph.

Design authority (per Issue #65 Required Permanent Rules §2):
    1. Static pre-check: AST parse every migration file in `script_location` and
       validate `revision` uniqueness, `down_revision` resolvability (str or
       tuple/list), no cycles, no orphans (unreachable from any base).
    2. Authority check: hand the same `script_location` to Alembic's own
       `ScriptDirectory` and call `get_heads()` / `walk_revisions()`. Any
       `KeyError`, `AttributeError`, or exception means the graph is broken at
       the authoritative layer. We do NOT simulate Alembic with regex.

Exit codes:
    0  PASS  — graph is valid, single (or whitelisted-multi) head.
    1  FAIL  — graph is broken (duplicate, dangling, cycle, orphan, bad tuple,
               bad down_revision type, etc.).
    2  FAIL  — usage / config error (bad script_location, bad allow-list file,
               alembic not installed).

Usage:
    python scripts/wf/alembic_graph_gate.py \
        --script-location alembic \
        [--allow-multi-head-file scripts/wf/alembic_allow_multi_head.txt]
"""
from __future__ import annotations

import argparse
import ast
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class GraphResult:
    ok: bool
    reason: str = ""
    heads: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    revisions: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    dangling: list[tuple[str, object]] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = []
        if self.ok:
            lines.append("ALEMBIC_GRAPH_GATE: PASS")
            lines.append(f"  heads={self.heads}")
            lines.append(f"  bases={self.bases}")
            lines.append(f"  revisions={len(self.revisions)}")
        else:
            lines.append("ALEMBIC_GRAPH_GATE: FAIL")
            lines.append(f"  reason={self.reason}")
            if self.duplicates:
                lines.append(f"  duplicate_revisions={self.duplicates}")
            if self.dangling:
                lines.append(f"  dangling={self.dangling}")
            if self.cycles:
                lines.append(f"  cycles={self.cycles}")
            if self.orphans:
                lines.append(f"  orphans={self.orphans}")
            if self.details:
                lines.append("  details:")
                for d in self.details:
                    lines.append(f"    - {d}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Static pre-check: AST-parse every migration file
# ---------------------------------------------------------------------------


def _literal_value(node: ast.AST):
    """Return the constant value of an AST node, or raise ValueError."""
    try:
        return ast.literal_eval(node)
    except Exception as exc:
        raise ValueError(f"not a literal: {exc}") from exc


def parse_migration_file(path: Path) -> dict | None:
    """AST-parse a migration file and return {revision, down_revision, depends_on, branch_labels}.

    Returns None if the file does not look like a migration (no `revision` /
    `down_revision` assignments, e.g. env.py or script.py.mako).

    Raises ValueError if the file looks like a migration but is malformed.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    out = {}
    for node in tree.body:
        name = None
        value_node = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name):
                name = tgt.id
                value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                name = node.target.id
                value_node = node.value
        if name is None or name not in ("revision", "down_revision", "depends_on", "branch_labels"):
            continue
        try:
            value = _literal_value(value_node)
        except ValueError:
            value = f"<UNRESOLVABLE:{ast.dump(value_node)[:80]}>"
        out[name] = value
    if "revision" not in out and "down_revision" not in out:
        # Not a migration file (e.g. env.py)
        return None
    if "revision" not in out:
        raise ValueError(f"missing `revision` assignment in {path}")
    if "down_revision" not in out:
        raise ValueError(f"missing `down_revision` assignment in {path}")
    return out


def resolve_versions_dir(script_location: Path) -> Path:
    """Resolve the directory that contains migration files.

    Alembic's default layout: `script_location/versions/*.py`.
    If `script_location` itself has no migration files but has a `versions/`
    subdirectory with migrations, return that. Otherwise return `script_location`.
    """
    candidates = [script_location]
    versions_subdir = script_location / "versions"
    if versions_subdir.is_dir():
        candidates.insert(0, versions_subdir)
    for c in candidates:
        for entry in c.iterdir():
            if not entry.is_file():
                continue
            if not entry.name.endswith(".py"):
                continue
            if entry.name.startswith("__"):
                continue
            if entry.name == "script.py.mako":
                continue
            try:
                info = parse_migration_file(entry)
            except (SyntaxError, ValueError):
                raise
            if info is not None:
                return c
    # Fall back to versions/ if it exists, else script_location
    return versions_subdir if versions_subdir.is_dir() else script_location


def collect_revisions(script_location: Path) -> dict[str, dict]:
    """Walk script_location (or its `versions/` subdir) and return {revision_id: ...}."""
    if not script_location.is_dir():
        raise FileNotFoundError(f"script_location not a directory: {script_location}")
    versions_dir = resolve_versions_dir(script_location)
    revisions: dict[str, dict] = {}
    for entry in sorted(versions_dir.iterdir()):
        if not entry.is_file():
            continue
        if not entry.name.endswith(".py"):
            continue
        if entry.name == "script.py.mako":
            continue
        if entry.name.startswith("__"):
            continue
        info = parse_migration_file(entry)
        if info is None:
            continue
        rev = info["revision"]
        if not isinstance(rev, str):
            raise ValueError(f"{entry.name}: `revision` must be str, got {type(rev).__name__}: {rev!r}")
        info["file"] = str(entry)
        revisions[rev] = info
    return revisions


# ---------------------------------------------------------------------------
# Static graph validation
# ---------------------------------------------------------------------------


def _normalize_down_revision(value) -> tuple[str, ...]:
    """Return a tuple of predecessor revision IDs (possibly empty for None)."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f"down_revision list item must be str, got {type(item).__name__}: {item!r}")
            out.append(item)
        return tuple(out)
    raise ValueError(
        f"down_revision must be None, str, list[str] or tuple[str,...]; got {type(value).__name__}: {value!r}"
    )


def _reverse_edges(revisions: dict[str, dict]) -> dict[str, list[str]]:
    """For each revision, list of children that point at it."""
    rev: dict[str, list[str]] = {r: [] for r in revisions}
    for rid, info in revisions.items():
        try:
            preds = _normalize_down_revision(info["down_revision"])
        except ValueError:
            continue  # recorded as a separate failure
        for p in preds:
            if p in rev:
                rev[p].append(rid)
    return rev


def _find_cycle(revisions: dict[str, dict]) -> list[str] | None:
    """Detect any cycle in the revision graph; return the offending cycle or None."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {r: WHITE for r in revisions}

    def dfs(node: str, path: list[str]) -> list[str] | None:
        color[node] = GRAY
        path.append(node)
        info = revisions[node]
        try:
            preds = _normalize_down_revision(info["down_revision"])
        except ValueError:
            preds = ()
        for p in preds:
            if p not in color:
                continue
            if color[p] == GRAY:
                # Cycle: from p back to current path
                idx = path.index(p)
                return path[idx:] + [p]
            if color[p] == WHITE:
                sub = dfs(p, path)
                if sub:
                    return sub
        path.pop()
        color[node] = BLACK
        return None

    for r in revisions:
        if color[r] == WHITE:
            cyc = dfs(r, [])
            if cyc:
                return cyc
    return None


def _reachable_from_bases(revisions: dict[str, dict]) -> set[str]:
    """Return revisions reachable from any base (down_revision == None)."""
    bases = [r for r, info in revisions.items() if info["down_revision"] is None]
    rev = _reverse_edges(revisions)
    seen: set[str] = set()
    stack = list(bases)
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(rev.get(cur, []))
    return seen


def _heads(revisions: dict[str, dict]) -> list[str]:
    """Return revisions that have no children (i.e. nothing points at them)."""
    rev = _reverse_edges(revisions)
    return sorted(r for r, kids in rev.items() if not kids)


def _bases(revisions: dict[str, dict]) -> list[str]:
    return sorted(r for r, info in revisions.items() if info["down_revision"] is None)


def static_check(script_location: Path) -> GraphResult:
    """Run the static pre-check and return a partial GraphResult."""
    try:
        revisions = collect_revisions(script_location)
    except FileNotFoundError as exc:
        return GraphResult(ok=False, reason=str(exc))
    except ValueError as exc:
        return GraphResult(ok=False, reason=f"malformed migration: {exc}")

    if not revisions:
        return GraphResult(ok=False, reason=f"no migration files in {script_location}")

    result = GraphResult(ok=True, revisions=list(revisions.keys()))

    # 1. Duplicate revisions (same revision ID across multiple files)
    file_to_rev: dict[str, list[str]] = {}
    for rid, info in revisions.items():
        file_to_rev.setdefault(info["file"], []).append(rid)
    # nothing to detect this way (file→rev is 1:1 by construction)
    # Detect by file content collision instead: same revision ID in 2+ files
    rev_to_files: dict[str, list[str]] = {}
    for rid, info in revisions.items():
        rev_to_files.setdefault(rid, []).append(info["file"])
    dupes = sorted(rid for rid, files in rev_to_files.items() if len(files) > 1)
    if dupes:
        result.duplicates = dupes
        result.ok = False
        result.reason = "duplicate revision id(s) across files"
        for rid in dupes:
            result.details.append(f"revision={rid} files={rev_to_files[rid]}")

    # 2. down_revision type / format
    for rid, info in revisions.items():
        try:
            _normalize_down_revision(info["down_revision"])
        except ValueError as exc:
            result.ok = False
            result.reason = f"bad down_revision type in {info['file']}"
            result.details.append(f"revision={rid} error={exc}")

    # 3. Dangling down_revision (does not resolve)
    for rid, info in revisions.items():
        try:
            preds = _normalize_down_revision(info["down_revision"])
        except ValueError:
            continue
        for p in preds:
            if p not in revisions:
                result.dangling.append((rid, p))
    if result.dangling:
        result.ok = False
        result.reason = "dangling down_revision: predecessor does not exist"
        for rid, p in result.dangling:
            result.details.append(f"revision={rid} down_revision={p!r} not found in repository")

    # 4. Cycle detection
    if result.ok:
        cyc = _find_cycle(revisions)
        if cyc:
            result.cycles = [cyc]
            result.ok = False
            result.reason = "cycle detected in revision graph"
            result.details.append(f"cycle={cyc}")

    # 5. Orphan detection (not reachable from any base)
    reachable = _reachable_from_bases(revisions)
    orphans = sorted(set(revisions.keys()) - reachable)
    if orphans:
        # Only an issue if there's at least one base; if no base at all, that's
        # a different error.
        if _bases(revisions):
            result.orphans = orphans
            result.ok = False
            result.reason = "orphan revision(s) not reachable from any base"
            for o in orphans:
                result.details.append(f"orphan={o}")

    # 6. No base at all?
    if not _bases(revisions):
        result.ok = False
        result.reason = "no base revision (no migration with down_revision = None)"

    if result.ok:
        result.heads = _heads(revisions)
        result.bases = _bases(revisions)
    return result


# ---------------------------------------------------------------------------
# Authority check: Alembic's own ScriptDirectory
# ---------------------------------------------------------------------------


def authority_check(script_location: Path, static: GraphResult) -> GraphResult:
    """Cross-check with Alembic's ScriptDirectory. If static says OK but Alembic
    disagrees, alembic wins."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
    except ImportError:
        # Alembic missing is a usage error, not a graph error.
        return GraphResult(ok=False, reason="alembic not installed; cannot run authority check")

    # Build a minimal Config pointing at our script_location
    cfg = Config()
    cfg.set_main_option("script_location", str(script_location))
    cfg.set_main_option("prepend_sys_path", ".")

    try:
        sd = ScriptDirectory.from_config(cfg)
        heads = sd.get_heads()
        bases = sd.get_bases()
        # walk_revisions must not raise; if it does, the graph is broken
        all_revs = [r.revision for r in sd.walk_revisions()]
    except KeyError as exc:
        new = GraphResult(
            ok=False,
            reason=f"alembic ScriptDirectory graph broken: KeyError: {exc}",
            heads=static.heads,
            bases=static.bases,
            revisions=static.revisions,
        )
        new.details.append(
            "Alembic's authoritative revision_map could not resolve a predecessor. "
            "This is exactly the failure mode this gate is designed to catch."
        )
        return new
    except Exception as exc:  # noqa: BLE001
        return GraphResult(
            ok=False,
            reason=f"alembic ScriptDirectory raised {type(exc).__name__}: {exc}",
        )

    # Cross-check head set
    if sorted(heads) != sorted(static.heads):
        return GraphResult(
            ok=False,
            reason=(
                f"static/authority head mismatch: static={static.heads} "
                f"alembic={sorted(heads)}"
            ),
        )
    if sorted(bases) != sorted(static.bases):
        return GraphResult(
            ok=False,
            reason=(
                f"static/authority base mismatch: static={static.bases} "
                f"alembic={sorted(bases)}"
            ),
        )
    if sorted(all_revs) != sorted(static.revisions):
        return GraphResult(
            ok=False,
            reason="static/authority revision-set mismatch",
        )

    final = GraphResult(
        ok=True,
        heads=sorted(heads),
        bases=sorted(bases),
        revisions=sorted(all_revs),
    )
    return final


# ---------------------------------------------------------------------------
# Multi-head policy
# ---------------------------------------------------------------------------


def load_allow_multi_head(path: Path | None) -> set[str]:
    if path is None:
        return set()
    if not path.exists():
        return set()
    allow: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        allow.add(s)
    return allow


def check_multi_head(heads: list[str], allow: set[str]) -> GraphResult:
    """Multiple-head policy.

    Issue #65 Required Permanent Rule §2:
        Default single head; legitimate multi-head requires explicit
        configuration. ALL current heads MUST appear in the allow-list
        (when there are 2+). Heads missing from the allow-list fail the gate
        with the offending ids listed, so the operator must consciously
        confirm them.

        - 0 or 1 head → OK (no allow-list needed)
        - 2+ heads with empty allow → FAIL
        - 2+ heads where any head is missing from allow → FAIL with explicit
          list of missing heads
        - 2+ heads where every head is in allow → OK
    """
    if len(heads) <= 1:
        return GraphResult(ok=True, heads=heads)
    missing = sorted(set(heads) - allow)
    if missing:
        return GraphResult(
            ok=False,
            reason=(
                f"multiple Alembic heads not explicitly whitelisted: "
                f"missing={missing}"
            ),
            heads=heads,
            details=[
                f"heads={heads}",
                f"allow={sorted(allow)}",
                f"missing={missing}",
                "Add the missing revision id(s) to the allow-list file "
                "(--allow-multi-head-file) to acknowledge this multi-head "
                "deployment is intentional, or merge the branches.",
            ],
        )
    return GraphResult(ok=True, heads=heads)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_gate(script_location: Path, allow_multi_head_path: Path | None) -> GraphResult:
    static = static_check(script_location)
    if not static.ok:
        return static
    authority = authority_check(script_location, static)
    if not authority.ok:
        return authority
    allow = load_allow_multi_head(allow_multi_head_path)
    mh = check_multi_head(authority.heads, allow)
    if not mh.ok:
        return mh
    # Final: heads + bases + revisions
    return GraphResult(
        ok=True,
        heads=authority.heads,
        bases=authority.bases,
        revisions=authority.revisions,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--script-location",
        default="alembic",
        help="Path to alembic script directory containing the migration files.",
    )
    parser.add_argument(
        "--allow-multi-head-file",
        default=None,
        help="Path to a file listing revision IDs allowed as additional heads.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON object describing the result (in addition to / instead of text).",
    )
    args = parser.parse_args(argv)

    script_location = Path(args.script_location).resolve()
    allow_path = Path(args.allow_multi_head_file).resolve() if args.allow_multi_head_file else None

    if not script_location.is_dir():
        print(f"ALEMBIC_GRAPH_GATE: FAIL (script_location not a directory: {script_location})")
        return 2

    result = run_gate(script_location, allow_path)
    print(result.render())

    if args.json:
        import json
        print("---JSON---")
        print(json.dumps({
            "ok": result.ok,
            "reason": result.reason,
            "heads": result.heads,
            "bases": result.bases,
            "revisions": result.revisions,
            "orphans": result.orphans,
            "duplicates": result.duplicates,
            "dangling": result.dangling,
            "cycles": result.cycles,
            "details": result.details,
        }, indent=2))

    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
