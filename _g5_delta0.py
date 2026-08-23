"""G5 DB Setup: create head/base databases, parse junit, compute Delta."""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from sqlalchemy import create_engine, text


def g5_create_dbs() -> None:
    PG_ADMIN_URL = "postgresql+psycopg2://postgres:postgres@localhost:5432/postgres"
    eng = create_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        for db in ("pasay_head_g5", "pasay_base_g5"):
            try:
                c.execute(text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname='{db}' AND pid <> pg_backend_pid()"
                ))
            except Exception as e:
                print(f"term warn {db}: {e}")
            c.execute(text(f"DROP DATABASE IF EXISTS {db}"))
            c.execute(text(f"CREATE DATABASE {db}"))
            print(f"[G5-DB] {db} dropped + recreated.")
    eng.dispose()
    print("[G5-DB] OK.")


def parse_junit_failures(xml_path: Path) -> set:
    fails = set()
    if not xml_path.exists():
        print(f"[G5-PARSE] {xml_path} not found. Return empty.")
        return fails
    root = ET.parse(str(xml_path)).getroot()
    for tc in root.findall(".//testcase"):
        classname = tc.attrib.get("classname", "")
        name = tc.attrib.get("name", "")
        nodeid = f"{classname}::{name}" if classname else name
        for child in tc:
            if child.tag in ("failure", "error"):
                if nodeid:
                    fails.add(nodeid)
    print(f"[G5-PARSE] {xml_path.name}: {len(fails)} failures found.")
    for n in sorted(fails):
        print(f"    FAIL: {n}")
    return fails


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "createdbs"
    if mode == "createdbs":
        g5_create_dbs()
    elif mode == "parse":
        head_xml = Path(sys.argv[2]) if len(sys.argv) > 2 else "head_g5.xml"
        base_xml = Path(sys.argv[3]) if len(sys.argv) > 3 else "base_g5.xml"
        h = parse_junit_failures(Path(head_xml))
        b = parse_junit_failures(Path(base_xml))
        diff = h - b
        only_b = b - h
        print(f"[G5-DELTA] HEAD_FAIL={len(h)}  BASE_FAIL={len(b)}  HEAD-BASE={len(diff)}  BASE-HEAD={len(only_b)}")
        if diff:
            print("[G5-DELTA] HEAD-BASE non-empty (UNEXPECTED NEW FAILURES IN HEAD):")
            for n in sorted(diff):
                print("  - " + n)
            sys.exit(1)
        else:
            print("[G5-DELTA PASS] HEAD_FAIL - BASE_FAIL = EMPTY = 0 new failures. Delta=0 confirmed.")
            if only_b:
                print(f"[G5-DELTA INFO] BASE had {len(only_b)} failures NOT present in HEAD (fixed in HEAD - NOT a regression):")
                for n in sorted(only_b):
                    print("  FIXED (not regression): " + n)
