"""PASAY Alembic test fixtures (Issue #65).

Each subdirectory of `alembic_fixtures/` is a self-contained set of migration
files (in `versions/`), plus a `script.py.mako` and `env.py`, that exercises a
specific scenario for `alembic_graph_gate.py` and `alembic_safe_create.py`.

Layout per fixture:
    alembic_fixtures/<name>/
        env.py              — minimal env (no DB needed for static gate)
        script.py.mako      — copy of the project mako template
        versions/           — migration files for the scenario
"""
from __future__ import annotations
