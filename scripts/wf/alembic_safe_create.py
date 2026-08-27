"""PASAY Safe Alembic Migration Creation Helper (Issue #65 — PASAY-CI-ALEMBIC-SAFETY-P0-001).

Programmatic, narrow, fail-closed wrapper around `alembic revision` so that
agents NEVER hand-write `down_revision` from a filename stem or natural-language
description.

Permanent Rule §1 (Issue #65):
    Migration creation is a tool/graph operation, not a free-form text
    generation task. This helper reads the **real** Alembic head from the
    script directory and uses it as the predecessor. The caller may pass an
    explicit `--head` only when there are multiple heads (legitimate branch /
    merge migration) — never to override the graph.

Permanent Rule §3 (Issue #65):
    - Reads the current Alembic head(s).
    - 0 heads → predecessor = None (initial schema).
    - 1 head   → predecessor = head.
    - 2+ heads → fail closed unless `--head` is given and matches a current head.
    - Returns the new revision ID, predecessor, and the path of the created file.

Usage (programmatic):
    from scripts.wf.alembic_safe_create import safe_create_revision
    res = safe_create_revision("alembic", message="add foo")
    print(res.revision, res.predecessor, res.path)

Usage (CLI):
    python scripts/wf/alembic_safe_create.py \
        --script-location alembic --message "add foo"

Exit codes:
    0  PASS — new revision created (path printed).
    2  FAIL — graph is broken; safe creation is impossible.
    3  FAIL — caller passed an invalid `--head` (not in current heads).
    4  FAIL — script_location missing or no migrations.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SafeCreateResult:
    ok: bool
    revision: Optional[str] = None
    predecessor: Optional[str] = None
    heads_at_create: list[str] = None  # type: ignore[assignment]
    path: Optional[Path] = None
    error: str = ""

    def __post_init__(self):
        if self.heads_at_create is None:
            self.heads_at_create = []


class SafeCreateError(RuntimeError):
    """Raised when safe creation cannot proceed."""


def _resolve_versions_dir(script_location: Path) -> Path:
    """Mirror scripts/wf/alembic_graph_gate.py::resolve_versions_dir.

    alembic script_location often is `alembic/` while migrations live in
    `alembic/versions/`. Pick the first directory that actually contains a
    migration file.
    """
    from alembic_graph_gate import resolve_versions_dir
    return resolve_versions_dir(script_location)


def _read_current_heads(script_location: Path) -> list[str]:
    """Read current Alembic heads using alembic's own ScriptDirectory.

    Raises SafeCreateError if the graph is broken (alembic raises KeyError or
    similar — exactly the bug we must avoid).
    """
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
    except ImportError as exc:
        raise SafeCreateError(f"alembic not installed: {exc}") from exc

    cfg = Config()
    cfg.set_main_option("script_location", str(script_location))
    cfg.set_main_option("prepend_sys_path", ".")

    try:
        sd = ScriptDirectory.from_config(cfg)
        return list(sd.get_heads())
    except Exception as exc:  # noqa: BLE001
        raise SafeCreateError(
            f"alembic ScriptDirectory cannot parse the revision graph: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _generate_revision_id() -> str:
    """Generate a new Alembic revision ID. Uses alembic's own generator."""
    from alembic.util import rev_id
    return rev_id()


def _template_path() -> Path:
    """Locate the alembic `script.py.mako` template shipped with alembic."""
    import alembic
    candidate = Path(alembic.__file__).parent / "templates" / "generic" / "script.py.mako"
    if not candidate.exists():
        # Older alembic: try the standard location
        candidate = Path(alembic.__file__).parent / "script.py.mako"
    if not candidate.exists():
        raise SafeCreateError(f"cannot locate alembic script.py.mako template (alembic={alembic.__file__})")
    return candidate


def _render_mako(template: str, context: dict) -> str:
    """Render a migration file using an in-house deterministic template.

    We do NOT use mako here on purpose. The Issue #65 contract is "no free-form
    text generation" — every byte of the migration file must come from the
    helper, with the predecessor (and only the predecessor) coming from the
    authoritative Alembic graph read. Using mako's conditional expressions
    (`${imports if imports else ""}`) introduces a second layer of templating
    where `Undefined` and falsy values can leak into the output, defeating
    determinism. We render a fixed Python module instead.

    The output matches the alembic `script.py.mako` "generic" template's
    expected shape so that Alembic itself can re-import the file via
    `ScriptDirectory`.
    """
    rev = context["up_revision"]
    down = context["down_revision"]
    br = context["branch_labels"]
    dep = context["depends_on"]

    def _repr(v):
        if v is None:
            return "None"
        if isinstance(v, str):
            return repr(v)
        if isinstance(v, (list, tuple)):
            return "(" + ", ".join(repr(x) for x in v) + ")"
        return repr(v)

    docstring = (context.get("message") or "pasay safe_create_revision").replace('"""', '\\"\\"\\"')
    return (
        '"""' + docstring + '\n\n'
        'Revision ID: ' + str(rev) + '\n'
        'Revises: ' + (str(down) if down is not None else "") + '\n'
        'Create Date: ' + str(context.get("create_date", "")) + '\n\n'
        '"""\n'
        'from typing import Sequence, Union\n\n'
        'from alembic import op\n'
        'import sqlalchemy as sa\n\n'
        '# revision identifiers, used by Alembic.\n'
        'revision: str = ' + _repr(rev) + '\n'
        'down_revision: Union[str, Sequence[str], None] = ' + _repr(down) + '\n'
        'branch_labels: Union[str, Sequence[str], None] = ' + _repr(br) + '\n'
        'depends_on: Union[str, Sequence[str], None] = ' + _repr(dep) + '\n\n\n'
        'def upgrade() -> None:\n'
        '    pass\n\n\n'
        'def downgrade() -> None:\n'
        '    pass\n'
    )


def _resolve_predecessor(
    heads: list[str], explicit_head: Optional[str]
) -> Optional[str]:
    """Determine the predecessor for a new migration.

    Rules (Issue #65 Required Permanent Rule §3):
        - 0 heads → None
        - 1 head  → head
        - 2+ heads → require --head; fail closed otherwise
    """
    if len(heads) == 0:
        return None
    if len(heads) == 1:
        if explicit_head is not None and explicit_head != heads[0]:
            raise SafeCreateError(
                f"explicit --head={explicit_head!r} does not match the unique "
                f"current head {heads[0]!r}; refusing to create a diverging branch"
            )
        return heads[0]
    # Multiple heads: caller MUST pick one explicitly
    if explicit_head is None:
        raise SafeCreateError(
            f"multiple current Alembic heads {heads}; pass --head=<revision> "
            f"to specify which branch to extend"
        )
    if explicit_head not in heads:
        raise SafeCreateError(
            f"--head={explicit_head!r} is not among the current heads {heads}"
        )
    return explicit_head


def safe_create_revision(
    script_location: str | os.PathLike,
    message: str,
    head: Optional[str] = None,
    branch_label: Optional[str] = None,
    depends_on: Optional[str] = None,
) -> SafeCreateResult:
    """Create a new alembic revision safely.

    Parameters
    ----------
    script_location : path-like
        Path to alembic `script_location` (containing `env.py` and `versions/`).
    message : str
        Revision message (becomes the docstring).
    head : str, optional
        Explicit predecessor. Required when there are multiple current heads;
        forbidden to override the unique current head.
    branch_label : str, optional
        Branch label for merge migrations.
    depends_on : str, optional
        `depends_on` field for cross-branch dependencies.

    Returns
    -------
    SafeCreateResult
        Contains the new revision ID, predecessor, and the path of the file.
    """
    sl = Path(script_location).resolve()
    if not sl.is_dir():
        return SafeCreateResult(ok=False, error=f"script_location not a directory: {sl}")

    try:
        versions_dir = _resolve_versions_dir(sl)
        heads = _read_current_heads(sl)
    except SafeCreateError as exc:
        return SafeCreateResult(ok=False, error=str(exc))

    try:
        predecessor = _resolve_predecessor(heads, head)
    except SafeCreateError as exc:
        return SafeCreateResult(ok=False, error=str(exc), heads_at_create=heads)

    new_rev = _generate_revision_id()
    template = _template_path().read_text(encoding="utf-8")
    body = _render_mako(
        template,
        {
            "message": message,
            "up_revision": new_rev,
            "down_revision": predecessor,
            "branch_labels": branch_label,
            "depends_on": depends_on,
            "create_date": "",
            "imports": "",
            "upgrades": "pass",
            "downgrades": "pass",
        },
    )

    # Compute the target file name using the same slug rules alembic uses,
    # so the helper never invents its own filename convention. The default
    # alembic file_template is "${rev}_${slug}"; we mirror that here.
    import re as _re
    _slug_re = _re.compile(r"[^\w]+", _re.UNICODE)
    slug = "_".join(_slug_re.findall(message or "")).lower()
    truncate = 40
    if len(slug) > truncate:
        slug = slug[:truncate].rsplit("_", 1)[0] + "_"
    target_name = f"{new_rev}_{slug}.py" if slug else f"{new_rev}.py"

    target_path = versions_dir / target_name
    if target_path.exists():
        return SafeCreateResult(
            ok=False,
            error=f"target file already exists: {target_path}",
            heads_at_create=heads,
            revision=new_rev,
            predecessor=predecessor,
        )

    target_path.write_text(body, encoding="utf-8")

    return SafeCreateResult(
        ok=True,
        revision=new_rev,
        predecessor=predecessor,
        heads_at_create=heads,
        path=target_path,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--script-location",
        default="alembic",
        help="Path to alembic script directory.",
    )
    parser.add_argument(
        "--message",
        required=True,
        help="Revision message (becomes the docstring).",
    )
    parser.add_argument(
        "--head",
        default=None,
        help="Explicit predecessor. Required when multiple heads exist.",
    )
    parser.add_argument(
        "--branch-label",
        default=None,
        help="Branch label (for merge migrations).",
    )
    parser.add_argument(
        "--depends-on",
        default=None,
        help="depends_on value (str or comma-separated).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute predecessor / new revision ID without writing the file.",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        from alembic_graph_gate import static_check
        sl = Path(args.script_location).resolve()
        if not sl.is_dir():
            print(f"SAFE_CREATE: FAIL (script_location not a directory: {sl})")
            return 4
        try:
            heads = _read_current_heads(sl)
        except SafeCreateError as exc:
            print(f"SAFE_CREATE: FAIL ({exc})")
            return 2
        try:
            predecessor = _resolve_predecessor(heads, args.head)
        except SafeCreateError as exc:
            print(f"SAFE_CREATE: FAIL ({exc})")
            return 3
        new_rev = _generate_revision_id()
        print("SAFE_CREATE: DRY_RUN")
        print(f"  heads={heads}")
        print(f"  predecessor={predecessor}")
        print(f"  new_revision={new_rev}")
        return 0

    res = safe_create_revision(
        args.script_location,
        message=args.message,
        head=args.head,
        branch_label=args.branch_label,
        depends_on=args.depends_on,
    )
    if not res.ok:
        print(f"SAFE_CREATE: FAIL ({res.error})")
        print(f"  heads_at_create={res.heads_at_create}")
        if "explicit" in res.error or "is not among" in res.error:
            return 3
        if "not a directory" in res.error:
            return 4
        return 2

    print("SAFE_CREATE: PASS")
    print(f"  revision={res.revision}")
    print(f"  predecessor={res.predecessor}")
    print(f"  heads_at_create={res.heads_at_create}")
    print(f"  path={res.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
