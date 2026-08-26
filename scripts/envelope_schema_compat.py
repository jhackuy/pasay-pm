#!/usr/bin/env python3
"""PASAY DOOR-16 Envelope schema compatibility check.

Compares the Python Pydantic envelope schema (app/schemas/envelope.py)
with the TypeScript envelope contract (cloudflare-worker/src/envelope.ts)
to ensure the two sides stay byte-symmetric on the shared cross-boundary
fields and conventions.

Checks performed (FAIL = exit 1):
  1. Top-level envelope fields: version, kind, event_id, occurred_at, payload
     must exist on BOTH sides for each envelope kind.
  2. event_id prefix convention:
       telegram_update  → "tg:" prefix validated on BOTH sides.
       scheduled_job    → "sched:" prefix validated on BOTH sides.
  3. _telegram_meta alias: Python must expose the field via Pydantic alias
     `_telegram_meta` matching the TS property name.
  4. ENVELOPE_VERSION literal must be equal ("1").
  5. Kind literal values ("telegram_update", "scheduled_job") must match.
  6. ScheduledJob payload shape: job_name / scheduled_at / params on BOTH sides.

Exit codes:
  0  all checks pass
  1  at least one compatibility issue found
  2  invocation / file-read / parse errors
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


PY_ENVELOPE_DEFAULT = "app/schemas/envelope.py"
TS_ENVELOPE_DEFAULT = "cloudflare-worker/src/envelope.ts"


# ---------- Python side extraction (AST-lite regex, no import needed) ----------

PY_VERSION_RE = re.compile(r'ENVELOPE_VERSION\s*:\s*str\s*=\s*"([^"]+)"', re.M)
PY_ENUM_KIND_RE = re.compile(
    r"class\s+EnvelopeKind\s*\(\s*str\s*,\s*Enum\s*\)\s*:"
    r"(?P<body>(?:[^\n]*\n)+?(?=\nclass\s|\Z))",
)
PY_KIND_VALUE_RE = re.compile(r'([A-Z_]+)\s*=\s*"([^"]+)"')

PY_TG_ENVELOPE_RE = re.compile(
    r"class\s+TelegramUpdateEnvelope\s*\(\s*BaseModel\s*\)\s*:"
    r"(?P<body>.*?)(?=\nclass\s+\w+|\Z)",
    re.S,
)
PY_SCH_ENVELOPE_RE = re.compile(
    r"class\s+ScheduledJobEnvelope\s*\(\s*BaseModel\s*\)\s*:"
    r"(?P<body>.*?)(?=\nclass\s+\w+|\Z)",
    re.S,
)
PY_SCH_PAYLOAD_RE = re.compile(
    r"class\s+ScheduledJobPayload\s*\(\s*BaseModel\s*\)\s*:"
    r"(?P<body>.*?)(?=\nclass\s+\w+|\Z)",
    re.S,
)

PY_FIELD_RE = re.compile(
    r"^\s+(\w+)\s*:\s*([^\n=]+?)(?:\s*=\s*(?:Field\([^)]*\)|[^,\n]+?))?\s*$",
    re.M,
)
PY_ALIAS_RE = re.compile(
    r"telegram_meta[\s\S]{0,400}?\balias\s*=\s*[\"']_telegram_meta[\"']",
    re.S,
)
PY_SERIAL_ALIAS_RE = re.compile(
    r"telegram_meta[\s\S]{0,400}?serialization_alias\s*=\s*[\"']_telegram_meta[\"']",
    re.S,
)

PY_EVENT_ID_PREFIX_TG = re.compile(
    r"def\s+_event_id_prefix\s*\(.*?\n.*?startswith\s*\(\s*[\"']tg:[\"']\s*\)",
    re.S,
)
PY_EVENT_ID_PREFIX_SCHED = re.compile(
    r"def\s+_event_id_prefix\s*\(.*?\n.*?startswith\s*\(\s*[\"']sched:[\"']\s*\)",
    re.S,
)


# ---------- TypeScript side extraction ----------

TS_VERSION_RE = re.compile(
    r'export\s+const\s+ENVELOPE_VERSION\s*=\s*"([^"]+)"'
)
TS_KIND_TYPE_RE = re.compile(
    r"export\s+type\s+EnvelopeKind\s*=\s*(?P<body>(?:[^;])+);",
    re.S,
)
TS_KIND_VALUE_RE = re.compile(r'"([^"]+)"')

TS_BASE_ENVELOPE_RE = re.compile(
    r"export\s+interface\s+BaseEnvelope\s*\{(?P<body>.+?)(?=\n\})",
    re.S,
)
TS_TG_ENVELOPE_RE = re.compile(
    r"export\s+interface\s+TelegramUpdateEnvelope\s+extends\s+BaseEnvelope\s*\{(?P<body>.+?)(?=\n\}\s*\n)",
    re.S,
)
TS_SCH_ENVELOPE_RE = re.compile(
    r"export\s+interface\s+ScheduledJobEnvelope\s+extends\s+BaseEnvelope\s*\{(?P<body>.+?)(?=\n\}\s*\n)",
    re.S,
)

TS_FIELD_RE = re.compile(
    r"^\s*(\w+)\??\s*:\s*([^;\n]+);",
    re.M,
)
TS_NESTED_PAYLOAD_SCHED_RE = re.compile(
    r"payload\s*:\s*\{\s*(?P<body>.*?)\s*\};",
    re.S,
)
TS_NESTED_TELEGRAM_META_RE = re.compile(
    r"_telegram_meta\s*:\s*\{(?P<body>.*?)\};",
    re.S,
)

TS_TG_MAKE_EVENT_ID = re.compile(r"return\s*`tg:\$\{")
TS_SCH_MAKE_EVENT_ID = re.compile(r"return\s*`sched:\$\{")


def extract_python_fields_from_class_body(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for m in PY_FIELD_RE.finditer(body):
        name, type_str = m.group(1), m.group(2).strip()
        if name.startswith("_"):
            continue
        fields[name] = type_str.rstrip(", ").strip()
    return fields


def extract_ts_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for m in TS_FIELD_RE.finditer(body):
        fields[m.group(1)] = m.group(2).strip()
    return fields


def parse_py(src: str) -> dict:
    version_m = PY_VERSION_RE.search(src)
    version = version_m.group(1) if version_m else None

    kind_enum: dict[str, str] = {}
    kind_m = PY_ENUM_KIND_RE.search(src)
    if kind_m:
        for kv in PY_KIND_VALUE_RE.finditer(kind_m.group("body")):
            kind_enum[kv.group(1)] = kv.group(2)

    tg_body_m = PY_TG_ENVELOPE_RE.search(src)
    tg_fields = extract_python_fields_from_class_body(tg_body_m.group("body")) if tg_body_m else {}

    sch_body_m = PY_SCH_ENVELOPE_RE.search(src)
    sch_fields = extract_python_fields_from_class_body(sch_body_m.group("body")) if sch_body_m else {}

    sch_payload_m = PY_SCH_PAYLOAD_RE.search(src)
    sch_payload_fields = (
        extract_python_fields_from_class_body(sch_payload_m.group("body"))
        if sch_payload_m else {}
    )

    has_tg_alias = bool(PY_ALIAS_RE.search(src))
    has_tg_serial_alias = bool(PY_SERIAL_ALIAS_RE.search(src))

    return {
        "version": version,
        "kind_enum": kind_enum,
        "telegram_update_fields": tg_fields,
        "scheduled_job_fields": sch_fields,
        "scheduled_payload_fields": sch_payload_fields,
        "telegram_meta_alias": has_tg_alias,
        "telegram_meta_serialization_alias": has_tg_serial_alias,
        "tg_event_id_prefix_validated": bool(PY_EVENT_ID_PREFIX_TG.search(src)),
        "sched_event_id_prefix_validated": bool(PY_EVENT_ID_PREFIX_SCHED.search(src)),
    }


def parse_ts(src: str) -> dict:
    version_m = TS_VERSION_RE.search(src)
    version = version_m.group(1) if version_m else None

    kinds: list[str] = []
    kind_m = TS_KIND_TYPE_RE.search(src)
    if kind_m:
        kinds = [m.group(1) for m in TS_KIND_VALUE_RE.finditer(kind_m.group("body"))]

    base_body_m = TS_BASE_ENVELOPE_RE.search(src)
    base_fields = extract_ts_fields(base_body_m.group("body")) if base_body_m else {}

    tg_body_m = TS_TG_ENVELOPE_RE.search(src)
    tg_local = extract_ts_fields(tg_body_m.group("body")) if tg_body_m else {}
    has_tg_meta_field = False
    if tg_body_m:
        has_tg_meta_field = bool(TS_NESTED_TELEGRAM_META_RE.search(tg_body_m.group("body")))
    if has_tg_meta_field and "_telegram_meta" not in tg_local:
        tg_local["_telegram_meta"] = "object"

    sch_body_m = TS_SCH_ENVELOPE_RE.search(src)
    sch_fields = extract_ts_fields(sch_body_m.group("body")) if sch_body_m else {}

    sch_payload_fields: dict[str, str] = {}
    sch_payload_top_level_present = False
    if sch_body_m:
        payload_match = TS_NESTED_PAYLOAD_SCHED_RE.search(sch_body_m.group("body"))
        if payload_match:
            sch_payload_fields = extract_ts_fields(payload_match.group("body"))
            sch_payload_top_level_present = True
    if sch_payload_top_level_present and "payload" not in sch_fields:
        sch_fields["payload"] = "object"

    tg_fields = {**base_fields, **tg_local}
    scheduled_fields = {**base_fields, **sch_fields}

    return {
        "version": version,
        "kind_literals": sorted(set(kinds)),
        "base_fields": base_fields,
        "telegram_update_fields": tg_fields,
        "scheduled_job_fields": scheduled_fields,
        "scheduled_payload_fields": sch_payload_fields,
        "tg_make_event_id_uses_tg_prefix": bool(TS_TG_MAKE_EVENT_ID.search(src)),
        "sched_make_event_id_uses_sched_prefix": bool(TS_SCH_MAKE_EVENT_ID.search(src)),
    }


REQUIRED_ENVELOPE_FIELDS = ["version", "kind", "event_id", "occurred_at", "payload"]
SCHEDULED_PAYLOAD_FIELDS = ["job_name", "scheduled_at", "params"]


def compare(py: dict, ts: dict) -> dict:
    issues: list[str] = []
    findings: list[str] = []

    if py["version"] != ts["version"]:
        issues.append(
            f"ENVELOPE_VERSION mismatch: Python={py['version']!r} TypeScript={ts['version']!r}"
        )
    else:
        findings.append(f"ENVELOPE_VERSION match: {py['version']!r}")

    py_kind_values = set(py["kind_enum"].values())
    ts_kind_values = set(ts["kind_literals"])
    if py_kind_values != ts_kind_values:
        issues.append(
            f"EnvelopeKind literals mismatch: Python-only={py_kind_values - ts_kind_values} "
            f"TypeScript-only={ts_kind_values - py_kind_values}"
        )
    else:
        findings.append(f"EnvelopeKind literals match: {sorted(py_kind_values)}")

    # ----- telegram_update cross-boundary field set -----
    for f in REQUIRED_ENVELOPE_FIELDS:
        py_has = f in py["telegram_update_fields"]
        ts_has = f in ts["telegram_update_fields"]
        if not (py_has and ts_has):
            issues.append(
                f"telegram_update missing required field {f!r}: "
                f"Python={'YES' if py_has else 'NO'} TypeScript={'YES' if ts_has else 'NO'}"
            )
    findings.append(
        "telegram_update fields match required: "
        + ", ".join(
            f"{f}" for f in REQUIRED_ENVELOPE_FIELDS
            if f in py["telegram_update_fields"] and f in ts["telegram_update_fields"]
        )
    )

    # ----- scheduled_job cross-boundary field set -----
    for f in REQUIRED_ENVELOPE_FIELDS:
        py_has = f in py["scheduled_job_fields"]
        ts_has = f in ts["scheduled_job_fields"]
        if not (py_has and ts_has):
            issues.append(
                f"scheduled_job missing required field {f!r}: "
                f"Python={'YES' if py_has else 'NO'} TypeScript={'YES' if ts_has else 'NO'}"
            )
    findings.append(
        "scheduled_job fields match required: "
        + ", ".join(
            f"{f}" for f in REQUIRED_ENVELOPE_FIELDS
            if f in py["scheduled_job_fields"] and f in ts["scheduled_job_fields"]
        )
    )

    # ----- scheduled_job payload shape -----
    for f in SCHEDULED_PAYLOAD_FIELDS:
        py_has = f in py["scheduled_payload_fields"]
        ts_has = f in ts["scheduled_payload_fields"]
        if not (py_has and ts_has):
            issues.append(
                f"scheduled_job payload missing field {f!r}: "
                f"Python={'YES' if py_has else 'NO'} TypeScript={'YES' if ts_has else 'NO'}"
            )
    findings.append(
        "scheduled_job payload fields match: "
        + ", ".join(
            f"{f}" for f in SCHEDULED_PAYLOAD_FIELDS
            if f in py["scheduled_payload_fields"] and f in ts["scheduled_payload_fields"]
        )
    )

    # ----- event_id prefix convention -----
    if not (py["tg_event_id_prefix_validated"] and ts["tg_make_event_id_uses_tg_prefix"]):
        issues.append(
            "tg: prefix convention mismatch on telegram_update event_id: "
            f"Python validator={'YES' if py['tg_event_id_prefix_validated'] else 'NO'} "
            f"TypeScript make_event_id={'YES' if ts['tg_make_event_id_uses_tg_prefix'] else 'NO'}"
        )
    else:
        findings.append("telegram_update event_id prefix 'tg:' symmetric on both sides")

    if not (py["sched_event_id_prefix_validated"] and ts["sched_make_event_id_uses_sched_prefix"]):
        issues.append(
            "sched: prefix convention mismatch on scheduled_job event_id: "
            f"Python validator={'YES' if py['sched_event_id_prefix_validated'] else 'NO'} "
            f"TypeScript make_event_id={'YES' if ts['sched_make_event_id_uses_sched_prefix'] else 'NO'}"
        )
    else:
        findings.append("scheduled_job event_id prefix 'sched:' symmetric on both sides")

    # ----- _telegram_meta alias -----
    ts_has_tg_meta_field = "_telegram_meta" in ts["telegram_update_fields"]
    py_has_alias = py["telegram_meta_alias"]
    py_has_serial_alias = py["telegram_meta_serialization_alias"]
    if not ts_has_tg_meta_field:
        issues.append("TypeScript TelegramUpdateEnvelope missing '_telegram_meta' field")
    if not py_has_alias:
        issues.append("Python TelegramUpdateEnvelope missing alias='_telegram_meta' on telegram_meta")
    if not py_has_serial_alias:
        issues.append("Python TelegramUpdateEnvelope missing serialization_alias='_telegram_meta' on telegram_meta")
    if ts_has_tg_meta_field and py_has_alias and py_has_serial_alias:
        findings.append("_telegram_meta alias symmetric: TS field ↔ Python alias/serialization_alias")

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "findings": findings,
    }


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    json_only = "--json-only" in argv
    if len(args) == 0:
        root = Path.cwd()
        py_path = root / PY_ENVELOPE_DEFAULT
        ts_path = root / TS_ENVELOPE_DEFAULT
    elif len(args) == 1:
        root = Path(args[0]).resolve()
        py_path = root / PY_ENVELOPE_DEFAULT
        ts_path = root / TS_ENVELOPE_DEFAULT
    elif len(args) == 2:
        py_path = Path(args[0]).resolve()
        ts_path = Path(args[1]).resolve()
    else:
        print(
            f"Usage: {argv[0]} [--json-only] [project_root] [py_envelope.py ts_envelope.ts]",
            file=sys.stderr,
        )
        return 2

    try:
        py_src = py_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"ERROR: cannot read Python envelope {py_path}: {e}", file=sys.stderr)
        return 2
    try:
        ts_src = ts_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"ERROR: cannot read TypeScript envelope {ts_path}: {e}", file=sys.stderr)
        return 2

    py = parse_py(py_src)
    ts = parse_ts(ts_src)
    result = compare(py, ts)

    report = {
        "files": {
            "python": str(py_path),
            "typescript": str(ts_path),
        },
        "python_extracted": py,
        "typescript_extracted": ts,
        "ok": result["ok"],
        "issues": result["issues"],
        "findings": result["findings"],
    }

    def _out(s: str) -> None:
        sys.stdout.buffer.write((s + "\n").encode("utf-8", errors="replace"))

    if json_only:
        sys.stdout.buffer.write(
            (json.dumps(report, indent=2, ensure_ascii=False) + "\n")
            .encode("utf-8", errors="replace")
        )
    else:
        _out("PASAY Envelope Schema Compatibility Check")
        _out(f"  Python:     {py_path}")
        _out(f"  TypeScript: {ts_path}")
        _out("")
        _out("Findings:")
        for f in result["findings"]:
            _out(f"  [OK] {f}")
        _out("")
        if result["issues"]:
            _out("Issues (FAIL):")
            for i in result["issues"]:
                _out(f"  [!!] {i}")
            _out(f"\nCOMPAT: FAIL ({len(result['issues'])} issue(s))")
        else:
            _out("COMPAT: PASS (0 issues)")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
