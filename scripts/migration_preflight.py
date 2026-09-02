#!/usr/bin/env python3
"""Graph-aware Alembic migration preflight.

This module is the delivery preflight for ``.github/workflows/deploy.yml``.
It replaces the previous hard-coded ``EXPECTED_REVISION = "0001_baseline"``
check (run #354 / PR #117 review) with a graph-aware check that:

1. Reads the live Alembic rewrite chain from ``alembic/versions/*.py`` via
   ``alembic.script.ScriptDirectory`` — the same source of truth ``alembic``
   itself uses at deploy time. ``EXPECTED_REVISION`` therefore stops being
   a literal string the developer must remember to bump every time a new
   migration is added.

2. Inspects the recorded ``alembic_version`` table on the target DB and
   classifies it into exactly one of three outcomes:

   * ``PROCEED`` — ``alembic_version`` is empty, OR the single recorded
     revision is any ancestor or current head of the rewrite chain. The
     deploy step then runs ``alembic upgrade head`` normally.

   * ``LEGACY_RESET`` — the single recorded revision is exactly the
     retired legacy revision ``r3_grant_all_public_20260828`` (the only
     off-graph revision we explicitly recognise). The script drops the
     legacy relation objects in the public schema so ``alembic upgrade
     head`` can install the rewrite chain on a clean slate. Public
     schema owner / ACLs / default privileges are preserved; only
     legacy relation objects are removed (same scope as the original
     inline preflight in deploy.yml).

   * ``FAIL_CLOSED`` — ambiguous rewrite graph (zero or multiple heads
     on disk), multi-row ``alembic_version``, or a single recorded
     revision that is neither an ancestor/head of the rewrite chain nor
     the exact retired legacy revision. The script raises ``SystemExit``
     and writes nothing destructive to the database so the deploy step
     refuses to continue.

The single decision line is printed on stdout as
``preflight_decision=<PROCEED|LEGACY_RESET> reason=<...> head=<revision>``
so the deploy step can parse it without coupling to logging format.

This script is the **only** path through which the deploy step mutates
the production DB before ``alembic upgrade head``; nothing else has
``workflows`` permission to touch ``.github/workflows/deploy.yml`` and
the rest of the deploy step does not run DDL.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Iterable, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from alembic.config import Config
from alembic.script import ScriptDirectory


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

# The one off-graph revision we explicitly recognise: the retired
# legacy revision from the pre-rewrite schema. This is the only revision
# that triggers LEGACY_RESET; any other unknown revision fails closed.
LEGACY_REVISION = "r3_grant_all_public_20260828"

# Pre-rewrite baseline kept as a *reference* marker only — the actual
# rewrite chain is discovered from disk at run time. ``EXPECTED_REVISION``
# is exported because callers / tests may still want to reference the
# pre-rewrite baseline as the "first step" of the rewrite chain.
EXPECTED_REVISION = "0001_baseline"

# Decisions that the script may return. String values are the canonical
# form emitted on stdout for the deploy step to parse.
PROCEED = "PROCEED"
LEGACY_RESET = "LEGACY_RESET"
FAIL_CLOSED = "FAIL_CLOSED"


# ----------------------------------------------------------------------
# Dataclasses
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightDecision:
    """Outcome of a preflight check.

    Attributes
    ----------
    action:
        One of ``PROCEED`` / ``LEGACY_RESET`` / ``FAIL_CLOSED``.
    reason:
        A short machine-readable reason code (e.g. ``"valid_rewrite_head"``,
        ``"multi_row_alembic_version"``). Surfaced in logs and stdout.
    head:
        The detected current head revision of the rewrite chain on disk,
        or ``None`` when the chain is ambiguous (zero or multiple heads).
    recorded:
        The recorded revisions actually found in ``alembic_version`` on
        the target DB. Useful for logging and assertions.
    dropped:
        The number of relation objects dropped during LEGACY_RESET.
        Always 0 for PROCEED / FAIL_CLOSED.
    """

    action: str
    reason: str
    head: str | None
    recorded: tuple[str, ...]
    dropped: int = 0


# ----------------------------------------------------------------------
# Rewrite chain discovery
# ----------------------------------------------------------------------


_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir),
)
_DEFAULT_ALEMBIC_INI = os.path.join(_REPO_ROOT, "alembic.ini")
_DEFAULT_VERSIONS_DIR = os.path.join(_REPO_ROOT, "alembic")


def _load_rewrite_chain(
    alembic_ini: str | None = None,
    versions_dir: str | None = None,
) -> tuple[set[str], str | None]:
    """Return ``(set_of_revisions, head_or_None)``.

    ``set_of_revisions`` includes every revision reachable from any
    current head via ``down_revision``. ``head_or_None`` is the single
    head revision if there is exactly one head, otherwise ``None`` to
    flag an ambiguous multi-head rewrite graph.
    """
    cfg = Config(alembic_ini or _DEFAULT_ALEMBIC_INI)
    if versions_dir is not None:
        cfg.set_main_option("script_location", versions_dir)
    scripts = ScriptDirectory.from_config(cfg)

    heads = list(scripts.get_heads())
    revisions: set[str] = set()
    # ``walk_revisions`` defaults to ``base="base", head="heads"`` which
    # walks every reachable revision from every head down to base. We
    # only need the unique set of revisions; the head ordering is
    # handled by ``get_heads()`` above.
    for rev in scripts.walk_revisions():
        revisions.add(rev.revision)

    if len(heads) == 1:
        return revisions, heads[0]
    return revisions, None


# ----------------------------------------------------------------------
# Recorded revisions on the target DB
# ----------------------------------------------------------------------


def _read_recorded_revisions(connection) -> tuple[str, ...]:
    """Return the rows of ``alembic_version`` on ``connection`` as a
    sorted tuple. Returns an empty tuple when the table does not exist
    (fresh DB or never-migrated DB).
    """
    has_table = connection.execute(
        text("SELECT to_regclass('public.alembic_version')"),
    ).scalar()
    if not has_table:
        return tuple()
    rows = connection.execute(
        text("SELECT version_num FROM alembic_version ORDER BY version_num"),
    ).scalars().all()
    return tuple(rows)


# ----------------------------------------------------------------------
# Legacy schema reset
# ----------------------------------------------------------------------


def _drop_legacy_schema(connection) -> int:
    """Drop every legacy relation in the public schema.

    Returns the number of relation objects dropped. The drop order is
    deterministic (views first, sequences last) to avoid dependency
    surprises. Public schema owner / ACLs / default privileges are
    preserved; only legacy relation objects are removed. This is the
    exact same SQL the previous inline preflight in ``deploy.yml`` ran.
    """
    relations = connection.execute(
        text(
            """
            SELECT c.relkind, c.relname
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relkind IN ('v', 'm', 'r', 'p', 'f', 'S')
            ORDER BY CASE c.relkind
                WHEN 'v' THEN 1
                WHEN 'm' THEN 2
                WHEN 'f' THEN 3
                WHEN 'p' THEN 4
                WHEN 'r' THEN 5
                WHEN 'S' THEN 6
            END
            """,
        ),
    ).all()
    object_types = {
        "v": "VIEW",
        "m": "MATERIALIZED VIEW",
        "r": "TABLE",
        "p": "TABLE",
        "f": "FOREIGN TABLE",
        "S": "SEQUENCE",
    }
    quote = connection.dialect.identifier_preparer.quote
    dropped = 0
    for relkind, relname in relations:
        connection.exec_driver_sql(
            f"DROP {object_types[relkind]} IF EXISTS "
            f"public.{quote(relname)} CASCADE",
        )
        dropped += 1
    return dropped


# ----------------------------------------------------------------------
# Main preflight entry point
# ----------------------------------------------------------------------


def run_preflight(
    database_url: str,
    *,
    dry_run: bool = False,
    alembic_ini: str | None = None,
    versions_dir: str | None = None,
    legacy_revision: str = LEGACY_REVISION,
) -> PreflightDecision:
    """Run the preflight against ``database_url``.

    Parameters
    ----------
    database_url:
        SQLAlchemy URL to the target PostgreSQL DB.
    dry_run:
        When ``True``, LEGACY_RESET will not actually drop the legacy
        schema; the decision still classifies correctly. Useful for CI
        and for callers that want to log without mutating.
    alembic_ini / versions_dir:
        Optional overrides for the script location (tests).
    legacy_revision:
        Override for the exact legacy revision we recognise as
        LEGACY_RESET. Defaults to ``r3_grant_all_public_20260828``.

    Returns
    -------
    PreflightDecision
        Outcome; callers should respect ``action`` and treat FAIL_CLOSED
        as a hard refusal.
    """
    chain, head = _load_rewrite_chain(
        alembic_ini=alembic_ini, versions_dir=versions_dir,
    )

    engine = create_engine(database_url)
    try:
        decision = _run_preflight_against(
            engine,
            chain=chain,
            head=head,
            legacy_revision=legacy_revision,
            dry_run=dry_run,
        )
    finally:
        engine.dispose()

    print(
        f"preflight_decision={decision.action} "
        f"reason={decision.reason} "
        f"head={decision.head or ''} "
        f"recorded={','.join(decision.recorded) or '<empty>'} "
        f"dropped={decision.dropped}"
    )
    return decision


def _run_preflight_against(
    engine: Engine,
    *,
    chain: set[str],
    head: str | None,
    legacy_revision: str,
    dry_run: bool,
) -> PreflightDecision:
    """Pure DB-bound preflight; reused by tests via a synthetic engine."""
    # Case 0: ambiguous rewrite graph (zero or multiple heads on disk).
    # Issue #112 requires fail-closed without mutation in this state, so
    # we refuse BEFORE touching the legacy schema even when the recorded
    # revision happens to equal LEGACY_REVISION. The deploy step's
    # ``set -euo pipefail`` will catch the SystemExit and abort without
    # running ``alembic upgrade head``.
    if head is None:
        raise SystemExit(
            "Refusing destructive reset: ambiguous rewrite graph "
            "(zero or multiple heads on disk)."
        )

    with engine.begin() as connection:
        recorded = _read_recorded_revisions(connection)

        # Case 1: empty alembic_version → first-time bootstrap.
        if not recorded:
            return PreflightDecision(
                action=PROCEED,
                reason="empty_alembic_version",
                head=head,
                recorded=(),
                dropped=0,
            )

        # Case 2: multi-row alembic_version → fail closed (DB corruption
        # or manual tampering). ``alembic upgrade head`` cannot reason
        # about multiple current revisions safely.
        if len(recorded) > 1:
            raise SystemExit(
                "Refusing destructive reset for multi-row alembic_version: "
                + ", ".join(recorded),
            )

        # Case 3: single recorded revision.
        rev = recorded[0]

        # 3a: rewrite chain ancestor/head → PROCEED. ``alembic upgrade
        # head`` will apply any missing ancestor migrations idempotently.
        if rev in chain:
            # The recorded revision may be the head itself, in which
            # case ``alembic upgrade head`` is a no-op; that's fine.
            return PreflightDecision(
                action=PROCEED,
                reason=(
                    "valid_rewrite_head" if rev == head
                    else "valid_rewrite_ancestor"
                ),
                head=head,
                recorded=recorded,
                dropped=0,
            )

        # 3b: exact retired legacy revision → LEGACY_RESET. Drop every
        # legacy relation object in the public schema so ``alembic
        # upgrade head`` can install the rewrite chain on a clean slate.
        if rev == legacy_revision:
            dropped = 0 if dry_run else _drop_legacy_schema(connection)
            return PreflightDecision(
                action=LEGACY_RESET,
                reason="exact_legacy_revision",
                head=head,
                recorded=recorded,
                dropped=dropped,
            )

        # 3c: any other single recorded revision that is neither an
        # ancestor/head of the rewrite chain nor the exact retired
        # legacy revision. This is the deterministic unknown /
        # off-graph branch from Issue #112.
        raise SystemExit(
            "Refusing destructive reset for unexpected Alembic revision: "
            + rev,
        )


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Graph-aware Alembic migration preflight for PASAY "
            ".github/workflows/deploy.yml"
        ),
    )
    parser.add_argument(
        "--database-url",
        required=True,
        help="SQLAlchemy URL to the target PostgreSQL DB",
    )
    parser.add_argument(
        "--alembic-ini",
        default=_DEFAULT_ALEMBIC_INI,
        help="Path to alembic.ini (default: repo root)",
    )
    parser.add_argument(
        "--versions-dir",
        default=_DEFAULT_VERSIONS_DIR,
        help="Path to alembic versions directory (default: repo root)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify only; do not actually drop legacy schema",
    )
    parser.add_argument(
        "--legacy-revision",
        default=LEGACY_REVISION,
        help="Override the retired legacy revision (tests only)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    try:
        decision = run_preflight(
            args.database_url,
            dry_run=args.dry_run,
            alembic_ini=args.alembic_ini,
            versions_dir=args.versions_dir,
            legacy_revision=args.legacy_revision,
        )
    except SystemExit:
        raise
    # FAIL_CLOSED is signalled via SystemExit from the lower layer;
    # PROCEED and LEGACY_RESET fall through to here.
    return 0 if decision.action in (PROCEED, LEGACY_RESET) else 1


if __name__ == "__main__":
    sys.exit(main())