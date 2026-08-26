#!/usr/bin/env python3
"""M005 Milestone C: Dependency residue grep scanner.

Walks the repository (excluding vendored / cache directories) looking for
imports, from-statements and requirements.txt entries that reference the
following legacy / forbidden middleware components:

    Redis, Kafka, Celery, Temporal, Queue2, Bot2

Emits a single JSON object on stdout with:

    {
      "scan_root": "...",
      "excluded_dirs": [...],
      "target_tokens": [...],
      "hit_count": N,
      "files": [
        {"path": "...", "hit_count": N, "matches": [{"line": N, "token": "...", "snippet": "..."}]},
        ...
      ],
      "summary_per_token": {"redis": N, "kafka": N, ...}
    }

This is a verification-only tool.  It NEVER writes redis.conf, celery.py,
docker-compose overrides, or any other residue file.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIR_NAMES = {
    ".git",
    "venv",
    ".venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".ai-control",
    "_m005_artifacts",
    "worktrees",
    ".runtime",
    ".trae",
}

TARGET_TOKENS = [
    ("redis", re.compile(r"(?i)(?:^|[\s\W])redis(?:$|[\s\W])")),
    ("kafka", re.compile(r"(?i)(?:^|[\s\W])kafka(?:$|[\s\W])")),
    ("celery", re.compile(r"(?i)(?:^|[\s\W])celery(?:$|[\s\W])")),
    ("temporal", re.compile(r"(?i)(?:^|[\s\W])temporal(?:$|[\s\W])")),
    ("queue2", re.compile(r"(?i)(?:^|[\s\W])queue2(?:$|[\s\W])")),
    ("bot2", re.compile(r"(?i)(?:^|[\s\W])bot2(?:$|[\s\W])")),
]

TEXT_FILE_SUFFIXES = {
    ".py", ".pyi",
    ".js", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".sh", ".bash", ".ps1", ".cmd", ".bat",
    ".env", ".example",
    ".dockerfile",
}

TEXT_FILE_NAMES = {
    "Dockerfile",
    "Makefile",
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    ".gitignore",
    ".env.example",
}


@dataclass
class Match:
    line: int
    token: str
    snippet: str


@dataclass
class FileHit:
    path: str
    hit_count: int = 0
    matches: list[Match] = field(default_factory=list)


def _is_text_candidate(p: Path) -> bool:
    name = p.name
    if name in TEXT_FILE_NAMES:
        return True
    return p.suffix.lower() in TEXT_FILE_SUFFIXES


def _walk(root: Path):
    import os

    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [
            d for d in dirnames
            if d not in EXCLUDED_DIR_NAMES
            and not d.startswith("pv-basetmp-")
        ]
        for fn in filenames:
            yield Path(dirpath) / fn


def scan() -> dict[str, Any]:
    files: list[FileHit] = []
    per_token_counts: dict[str, int] = {tok: 0 for tok, _ in TARGET_TOKENS}
    total_hits = 0

    SELF_NAME = Path(__file__).name
    GENERATED_MATRIX_MD_NAMES = {
        "m005_routers_scope_matrix.md",
    }
    for path in _walk(REPO_ROOT):
        if not _is_text_candidate(path):
            continue
        try:
            rel = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = str(path)
        if path.name == SELF_NAME:
            continue
        if path.name in GENERATED_MATRIX_MD_NAMES:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        fh = FileHit(path=rel)
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#") or stripped.startswith("//"):
                continue
            triple = False
            if stripped.startswith('"""') or stripped.startswith("'''"):
                triple = True
            if triple:
                continue
            if stripped.lower().startswith(("no ", "禁止", "不要", "禁用")):
                continue
            if "NO Redis" in stripped or "NO KAFKA" in stripped or "no extra services" in stripped.lower():
                continue
            if "M005" in stripped and ("Milestone" in stripped or "forbidden" in stripped or "残留" in stripped):
                continue
            for token, rx in TARGET_TOKENS:
                if rx.search(line):
                    snippet = stripped[:200]
                    fh.matches.append(Match(line=lineno, token=token, snippet=snippet))
                    fh.hit_count += 1
                    per_token_counts[token] += 1
                    total_hits += 1
        if fh.hit_count:
            files.append(fh)

    files.sort(key=lambda f: (-f.hit_count, f.path))

    return {
        "scan_root": str(REPO_ROOT),
        "excluded_dirs": sorted(EXCLUDED_DIR_NAMES),
        "target_tokens": [tok for tok, _ in TARGET_TOKENS],
        "hit_count": total_hits,
        "files_count": len(files),
        "files": [
            {
                "path": f.path,
                "hit_count": f.hit_count,
                "matches": [m.__dict__ for m in f.matches],
            }
            for f in files
        ],
        "summary_per_token": per_token_counts,
    }


def main() -> int:
    result = scan()
    buf = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    sys.stdout.buffer.write(buf.encode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
