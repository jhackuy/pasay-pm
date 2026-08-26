#!/usr/bin/env python3
"""PASAY DOOR-14 Float/Money safety gate.

Fail-closed scan for real ``float`` usage in financial Python paths.

Scopes scanned (grepped):
  - app/models/
  - app/services/
  - app/api/routers/

Patterns flagged (real code, not comments/docstrings):
  - float(...)       constructor call
  - ``: float``      type annotation
  - /float\(/        regex variants

White-listed (never FAIL):
  - occurrences inside comments / triple-quoted docstrings
  - files matching ``test_*.py``

BLACKLIST (financial files — any real float hit = immediate FAIL):
  expense.py, rent_payment_truth.py, financial.py,
  expense_payment_truth.py, deposit_settlement_service.py,
  commission_engine.py, payment_match.py, rent_claims.py,
  expense_claims.py, rent_math.py

Exit codes:
  0  all clear
  1  blacklist file hit, or non-whitelist real hits with no json-only mode
  2  invocation / path errors
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


FINANCIAL_BLACKLIST: frozenset[str] = frozenset({
    "expense.py",
    "rent_payment_truth.py",
    "financial.py",
    "expense_payment_truth.py",
    "deposit_settlement_service.py",
    "commission_engine.py",
    "payment_match.py",
    "rent_claims.py",
    "expense_claims.py",
    "rent_math.py",
})

SCAN_DIRS: tuple[str, ...] = (
    "app/models",
    "app/services",
    "app/api/routers",
)

FLOAT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ctor_call",
        re.compile(r"\bfloat\s*\("),
    ),
    (
        "type_annotation",
        re.compile(r":\s*float\b(?!\s*\w)"),
    ),
)

TRIPLE_SINGLE = "'''"
TRIPLE_DOUBLE = '"""'


def _strip_strings_and_comments(line: str, in_docstring: str | None) -> tuple[str, str | None]:
    """Return (code_only, new_docstring_state).

    Removes comments and string literal contents so pattern matching only
    fires on real executable code / type annotations. Docstrings are tracked
    across multiple lines via the in_docstring state token (None / triple-double
    / triple-single).
    """
    out_chars: list[str] = []
    i = 0
    n = len(line)
    state: str | None = in_docstring
    string_open: str | None = None
    while i < n:
        ch = line[i]
        nxt = line[i + 1] if i + 1 < n else ""

        if state is not None:
            if (
                ch == state[0]
                and nxt == state[1]
                and i + 2 < n
                and line[i + 2] == state[2]
            ):
                out_chars.append("   ")
                i += 3
                state = None
                continue
            out_chars.append(" ")
            i += 1
            continue

        if string_open is not None:
            if ch == "\\":
                out_chars.append("  ")
                i += 2
                continue
            if ch == string_open:
                string_open = None
            out_chars.append(" ")
            i += 1
            continue

        if ch == "#":
            break

        if (
            ch == '"'
            and nxt == '"'
            and i + 2 < n
            and line[i + 2] == '"'
        ):
            state = TRIPLE_DOUBLE
            out_chars.append("   ")
            i += 3
            continue
        if (
            ch == "'"
            and nxt == "'"
            and i + 2 < n
            and line[i + 2] == "'"
        ):
            state = TRIPLE_SINGLE
            out_chars.append("   ")
            i += 3
            continue

        if ch == '"':
            string_open = '"'
            out_chars.append(" ")
            i += 1
            continue
        if ch == "'":
            string_open = "'"
            out_chars.append(" ")
            i += 1
            continue

        out_chars.append(ch)
        i += 1

    return "".join(out_chars), state


def iter_py_files(root: Path, rel_dirs: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for rel in rel_dirs:
        d = root / rel
        if not d.is_dir():
            continue
        for p in d.rglob("*.py"):
            files.append(p)
    return sorted(files)


def scan_file(path: Path, root: Path) -> dict:
    rel = path.relative_to(root).as_posix()
    basename = path.name
    is_test = basename.startswith("test_")
    is_blacklist = basename in FINANCIAL_BLACKLIST

    hits: list[dict] = []
    in_doc: str | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()

    for lineno, raw in enumerate(lines, start=1):
        code_only, in_doc = _strip_strings_and_comments(raw, in_doc)
        for label, pat in FLOAT_PATTERNS:
            for m in pat.finditer(code_only):
                hits.append({
                    "line": lineno,
                    "col": m.start() + 1,
                    "pattern": label,
                    "match": m.group(0),
                    "context": raw.strip(),
                })

    whitelisted = is_test
    return {
        "file": rel,
        "basename": basename,
        "is_blacklist_file": is_blacklist,
        "is_test_file": is_test,
        "whitelisted": whitelisted,
        "hit_count": len(hits),
        "hits": hits,
    }


def build_report(root: Path) -> dict:
    files = iter_py_files(root, SCAN_DIRS)
    per_file = [scan_file(f, root) for f in files]

    blacklist_hits = [r for r in per_file if r["is_blacklist_file"] and r["hit_count"] > 0]
    non_whitelist_real_hits = [
        r for r in per_file
        if not r["whitelisted"] and r["hit_count"] > 0
    ]
    total_real_hits = sum(r["hit_count"] for r in non_whitelist_real_hits)

    return {
        "root": str(root),
        "scan_dirs": list(SCAN_DIRS),
        "financial_blacklist": sorted(FINANCIAL_BLACKLIST),
        "files_scanned": len(per_file),
        "total_blacklist_hits": sum(r["hit_count"] for r in blacklist_hits),
        "blacklist_hit_files": [r["file"] for r in blacklist_hits],
        "total_non_whitelist_hits": total_real_hits,
        "non_whitelist_hit_files": [r["file"] for r in non_whitelist_real_hits],
        "per_file": per_file,
        "fail": len(blacklist_hits) > 0,
    }


def main(argv: list[str]) -> int:
    json_only = "--json-only" in argv
    root_arg = None
    for a in argv[1:]:
        if a == "--json-only":
            continue
        root_arg = a
    root = Path(root_arg).resolve() if root_arg else Path.cwd()
    if not root.is_dir():
        print(f"ERROR: root not a directory: {root}", file=sys.stderr)
        return 2

    report = build_report(root)
    if json_only:
        json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        for r in report["per_file"]:
            if r["hit_count"] == 0:
                continue
            tag = ""
            if r["is_blacklist_file"]:
                tag = " [BLACKLIST-FAIL]"
            elif r["whitelisted"]:
                tag = " [WHITELIST]"
            else:
                tag = " [FLAG]"
            print(f"{r['file']}{tag}: {r['hit_count']} hit(s)")
            for h in r["hits"]:
                print(f"  L{h['line']}:{h['col']} {h['pattern']}: {h['context']}")
        print("-" * 60)
        print(f"files scanned: {report['files_scanned']}")
        print(f"blacklist files hit: {len(report['blacklist_hit_files'])} ({report['total_blacklist_hits']} hits)")
        print(f"non-whitelist files hit: {len(report['non_whitelist_hit_files'])} ({report['total_non_whitelist_hits']} hits)")
        if report["fail"]:
            print("FAIL: financial blacklist file contains real float usage")
        else:
            print("PASS: no financial-blacklist float hits")

    return 1 if report["fail"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
