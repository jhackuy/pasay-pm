"""PASAY-OPENCODE-QUAL-001 changed-files scope guard.

Pure-stdlib deterministic helper. Given a list of changed file paths and a
list of allow rules (POSIX-style globs with `**` recursive support), decides
whether the change is in-scope. Fail-closed: empty/invalid rules never
default to "allow everything".

This is a dev/CI/Agent governance helper. It is not used by production
runtime code paths and has no side effects on import.

CLI:
    python scripts/wf/wf_scope_guard.py \
        --files path1 path2 \
        --allow 'scripts/wf/**' \
        --json
    exit 0 -> PASS, exit != 0 -> FAIL (violations printed to stdout as JSON).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from typing import Iterable, Sequence


def normalize_path(path: str) -> str:
    """Normalize a path so Windows and POSIX spellings compare equal.

    - backslashes -> forward slashes
    - strip a single leading "./"
    - drop trailing whitespace; keep internal whitespace verbatim

    Intentionally does NOT call os.path.normpath: callers want a literal
    comparison, not a "resolve symlinks" pass. Repo-relative posix form is
    the canonical input convention.
    """
    if path is None:
        return ""
    p = path.replace("\\", "/").strip()
    if p.startswith("./"):
        p = p[2:]
    return p


def _glob_to_regex(pattern: str) -> str:
    """Translate a glob with `**` recursive support into a full-match regex.

    - `**` segment matches any number of path segments (including zero).
    - `*` matches any chars within a single segment (no '/').
    - `?` matches a single char within a segment.
    - Other characters are escaped.
    - The regex is anchored with a single trailing \\Z so segment-by-segment
      joining does not produce an invalid pattern.
    """
    parts = pattern.split("/")
    out: list[str] = []
    for part in parts:
        if part == "**":
            out.append(".*")
            continue
        translated = fnmatch.translate(part)
        # fnmatch.translate appends a trailing \Z; we strip it on every
        # segment and add exactly one anchor at the end.
        if translated.endswith("\\Z"):
            translated = translated[:-2]
        out.append(translated)
    return "/".join(out) + "\\Z"


def matches(rule: str, path: str) -> bool:
    """Return True iff the normalized path matches the normalized glob rule.

    A `**` anywhere in the rule triggers regex mode; pure-`*` rules use
    fnmatch directly. Empty rules never match anything.
    """
    np = normalize_path(path)
    nr = normalize_path(rule)
    if not nr:
        return False
    if not np:
        return False
    if "**" in nr:
        return re.fullmatch(_glob_to_regex(nr), np) is not None
    return fnmatch.fnmatchcase(np, nr)


def _rules_blank(rules: Sequence[str]) -> bool:
    if not rules:
        return True
    return all((r is None) or (not str(r).strip()) for r in rules)


def check_scope(changed_files: Iterable[str],
                allow_rules: Sequence[str]) -> dict:
    """Decide whether every changed file is covered by at least one rule.

    Returns a deterministic dict:

        {
          "ok": bool,
          "violations": list[str],   # sorted, normalized, deduped
          "missing_rules": bool,     # true iff rules were empty/invalid
          "reason": str              # empty when ok=True
        }

    Fail-closed: when allow_rules is empty/invalid AND there are any
    changed files, the result is ok=False and every changed file is
    reported as a violation with reason="allow_rules_empty_fail_closed".
    """
    rules_clean = [normalize_path(r) for r in (allow_rules or [])
                   if r is not None and str(r).strip()]
    changed_clean: list[str] = []
    seen: set[str] = set()
    for f in (changed_files or []):
        n = normalize_path(f)
        if not n or n in seen:
            continue
        seen.add(n)
        changed_clean.append(n)

    blank = _rules_blank(rules_clean) if not rules_clean else False

    if blank and changed_clean:
        return {
            "ok": False,
            "violations": sorted(changed_clean),
            "missing_rules": True,
            "reason": "allow_rules_empty_fail_closed",
        }

    if not changed_clean:
        return {
            "ok": True,
            "violations": [],
            "missing_rules": not rules_clean,
            "reason": "",
        }

    violations: list[str] = []
    for path in changed_clean:
        if not any(matches(r, path) for r in rules_clean):
            violations.append(path)
    violations.sort()
    return {
        "ok": not violations,
        "violations": violations,
        "missing_rules": not rules_clean,
        "reason": "" if not violations else "files_outside_allow_rules",
    }


def _emit_json(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Changed-files scope guard (OpenCode qualification slice).",
    )
    parser.add_argument("--files", nargs="*", default=[],
                        help="Repo-relative changed file paths.")
    parser.add_argument("--allow", action="append", default=[],
                        help="Glob allow rule (repeat for multiple).")
    parser.add_argument("--json", action="store_true",
                        help="Emit result JSON to stdout.")
    args = parser.parse_args(argv)

    result = check_scope(args.files, args.allow)
    if args.json:
        _emit_json(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())