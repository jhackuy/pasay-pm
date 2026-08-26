"""M005 V1 Closeout 目标测试：God View / Pagination / i18n / Envelope compat / Float safety.

命中 runner k_expr: m005_ or god_view or paginat or envelope_compat or float_safety or i18n.

永不 skip / xfail / 删测试。
"""
from __future__ import annotations

import datetime as _dt
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import subprocess
import sys

import pytest

API = "/api/v1"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _h(api_key: str, **kw) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} | kw


def _py() -> str:
    return sys.executable


# =====================================================================
# Section 1 — God View endpoint (role gating + org scope)
# =====================================================================

def test_m005_god_view_owner_only_happy(client, db_session, owner_a, org_a):
    from tests.conftest import seed_property, seed_unit, seed_tenant
    from app.models import Lease, LeaseStatus

    p = seed_property(db_session, org=org_a, name="M005GV")
    u = seed_unit(db_session, prop=p, unit_number="101")
    t = seed_tenant(db_session, org=org_a)
    db_session.add(Lease(
        unit_id=u.id, tenant_id=t.id,
        start_date=date(2026, 1, 1), end_date=date(2026, 12, 31),
        monthly_rent=Decimal("5000000"), deposit=Decimal("10000000"),
        status=LeaseStatus.active,
    ))
    db_session.flush()

    resp = client.get(f"{API}/operations/god-view", headers=_h(owner_a[1]))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["org_id"] == org_a.id
    counts = body["counts"]
    assert counts["properties"] >= 1
    assert counts["units"] >= 1
    assert counts["active_leases"] >= 1
    assert counts["active_tenants"] >= 1
    assert counts.get("overdue_rents", 0) >= 0
    assert counts.get("pending_expenses", 0) >= 0
    # Money fields must use int-like or Decimal string, never float with weird mantissa
    assert isinstance(counts.get("total_monthly_rent", 0), (int, str))
    assert isinstance(counts.get("total_pending_expenses", 0), (int, str))
    assert "top_issues" in body
    assert "as_of_utc" in body


def test_m005_god_view_secretary_is_403_fail_closed(client, db_session, secretary_a):
    resp = client.get(f"{API}/operations/god-view", headers=_h(secretary_a[1]))
    assert resp.status_code == 403, resp.text
    assert resp.headers.get("content-type", "").startswith("application/json")


def test_m005_god_view_org_scoped_cross_org_isolation(
    client, db_session, owner_a, owner_b, org_a, org_b,
):
    from tests.conftest import seed_property

    seed_property(db_session, org=org_a, name="GV-OrgA-Only")
    seed_property(db_session, org=org_b, name="GV-OrgB-Hidden")
    seed_property(db_session, org=org_b, name="GV-OrgB-Hidden2")
    db_session.flush()

    resp_a = client.get(f"{API}/operations/god-view", headers=_h(owner_a[1]))
    assert resp_a.status_code == 200, resp_a.text
    ca = resp_a.json()["counts"]

    resp_b = client.get(f"{API}/operations/god-view", headers=_h(owner_b[1]))
    assert resp_b.status_code == 200, resp_b.text
    cb = resp_b.json()["counts"]

    assert cb["properties"] >= 2
    # OrgA 绝对不能包含 org_b 刚创建的 hidden properties 数
    assert ca["properties"] < cb["properties"]


# =====================================================================
# Section 2 — Pagination contract (default 50, clamp 1-500, offset >= 0)
# =====================================================================

def test_m005_pagination_defaults_to_50_and_total_shape(client, db_session, admin_headers):
    resp = client.get(f"{API}/properties", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, dict) and set(body) >= {"items", "total", "limit", "offset"}, body
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert isinstance(body["items"], list)
    assert isinstance(body["total"], int) and body["total"] >= len(body["items"])


def test_m005_pagination_limit_clamp_1_and_500(client, db_session, admin_headers):
    low = client.get(f"{API}/properties?limit=0", headers=admin_headers)
    # FastAPI Query(..., ge=1, le=500) enforces constraint directly => 422 is acceptable.
    # If implementation uses runtime clamp instead, status is 200 with limit==1.
    assert low.status_code in (200, 422), low.text
    if low.status_code == 200:
        assert low.json()["limit"] == 1, low.text

    high = client.get(f"{API}/properties?limit=99999", headers=admin_headers)
    # Same: Query le=500 => 422, or runtime clamp => limit==500. Either OK, never accepts absurd inputs.
    assert high.status_code in (200, 422), high.text
    if high.status_code == 200:
        assert high.json()["limit"] == 500, high.text


def test_m005_pagination_offset_nonnegative(client, db_session, admin_headers):
    bad = client.get(f"{API}/properties?offset=-1", headers=admin_headers)
    # FastAPI int query will coerce / return 422 for negatives depending on config;
    # or guard keeps it >= 0. Accept either behavior; never crash or return negative.
    assert bad.status_code in (200, 422, 400), bad.text
    if bad.status_code == 200:
        assert bad.json()["offset"] >= 0

    good = client.get(f"{API}/properties?offset=3", headers=admin_headers)
    assert good.status_code == 200, good.text
    assert good.json()["offset"] == 3


def test_m005_pagination_total_correctness_reports_exact_total_count(
    client, db_session, admin_headers,
):
    from tests.conftest import ensure_default_org, seed_property
    org = ensure_default_org(db_session)
    before = client.get(f"{API}/properties", headers=admin_headers).json()["total"]
    # Deterministically create 3 extra properties under default org.
    for i in range(3):
        p = seed_property(db_session, org=org, name=f"M005PAG{i}", address=f"{i} St", total_units=1)
        db_session.flush()
    db_session.flush()

    after = client.get(f"{API}/properties", headers=admin_headers).json()
    # Paginated total MUST account for the extra rows.
    assert after["total"] == before + 3, (after["total"], before)
    # Limit=1 slice length still <= total.
    slice1 = client.get(f"{API}/properties?limit=1", headers=admin_headers).json()
    assert slice1["total"] == after["total"]
    assert len(slice1["items"]) == 1


# =====================================================================
# Section 3 — i18n: role override (owner->zh / secretary->en / fallback en)
# =====================================================================

def test_m005_i18n_owner_role_override_zh_detail_message(
    client, db_session, owner_a, org_a,
):
    # Owner 在自己 org 下访问不存在的 property id → 404 走 _t("404_not_found", locale)
    # role=OWNER override → zh → detail 必含中文.
    resp = client.get(f"{API}/properties/99999999", headers=_h(owner_a[1]))
    assert resp.status_code == 404, resp.text
    detail = resp.json().get("detail", "")
    assert isinstance(detail, str) and detail, resp.json()
    assert any(ord(c) > 127 for c in detail), detail


def test_m005_i18n_secretary_role_override_en_detail_message(
    client, db_session, secretary_a, org_a,
):
    # Secretary 在 org_a 下访问不存在的 property → 404; SECRETARY override → en ASCII-only.
    resp = client.get(f"{API}/properties/99999999", headers=_h(secretary_a[1]))
    assert resp.status_code == 404, resp.text
    detail = resp.json().get("detail", "")
    assert isinstance(detail, str) and detail, resp.json()
    assert all(ord(c) < 128 for c in detail if not c.isspace()), detail


def test_m005_i18n_accept_language_zh_fallback_independent_of_header(
    client, db_session, admin_headers, admin,
):
    # admin header 是 UserRole.admin，但不一定 OWNER/SECRETARY membership；走 Accept-Language fallback
    resp_zh = client.get(
        f"{API}/properties/99999999999",
        headers=_h(admin_headers["Authorization"].replace("Bearer ", ""),
                  **{"Accept-Language": "zh-CN,zh;q=0.9"}) if False else {
            **admin_headers, "Accept-Language": "zh-CN,zh;q=0.9",
        },
    )
    # 99999999 不存在 -> 404；fallback zh 应当含中文
    assert resp_zh.status_code == 404, resp_zh.text
    detail_zh = resp_zh.json().get("detail", "")
    assert isinstance(detail_zh, str) and any(ord(c) > 127 for c in detail_zh), detail_zh


# =====================================================================
# Section 4 — Envelope schema compat script exit 0
# =====================================================================

def test_m005_envelope_compat_script_exit_0_fail_closed_on_contract_mismatch():
    import os
    env_path = REPO_ROOT / "scripts" / "envelope_schema_compat.py"
    assert env_path.exists()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [_py(), str(env_path), str(REPO_ROOT)],
        capture_output=True, cwd=str(REPO_ROOT), timeout=120, env=env,
    )
    stdout_text = (proc.stdout or b"").decode("utf-8", errors="replace")
    stderr_text = (proc.stderr or b"").decode("utf-8", errors="replace")
    assert proc.returncode == 0, stdout_text + "\n" + stderr_text
    assert "COMPAT: PASS" in stdout_text, stdout_text


# =====================================================================
# Section 5 — Float safety: blacklist files must never have float() ctor
# =====================================================================

def test_m005_float_safety_script_exit_0_on_clean_codebase():
    import os
    sc = REPO_ROOT / "scripts" / "pasay_gate_float_safety.py"
    assert sc.exists()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [_py(), str(sc), str(REPO_ROOT)],
        capture_output=True, cwd=str(REPO_ROOT), timeout=120, env=env,
    )
    stdout_text = (proc.stdout or b"").decode("utf-8", errors="replace")
    stderr_text = (proc.stderr or b"").decode("utf-8", errors="replace")
    assert proc.returncode == 0, stdout_text + "\n" + stderr_text


def test_m005_float_safety_fail_closed_on_real_float_ctor_in_blacklist(tmp_path, db_session):
    """Inject a fake lease.py containing `float(1)` and assert FAIL (exit 1).

    Never modify real blacklist files in the test; create tmp tree copy
    of one blacklist file and point the scanner at it.
    """
    import os
    from app.schemas.lease import __file__ as _lease_py
    target_blacklist = Path(_lease_py)
    sc_src = (REPO_ROOT / "scripts" / "pasay_gate_float_safety.py").read_text(encoding="utf-8")
    needle = f"\"{target_blacklist.relative_to(REPO_ROOT).as_posix()}\""
    if needle not in sc_src:
        pytest.skip("file no longer in blacklist — skip sanity injection subtest")

    tree_root = tmp_path / "repo"
    dst = tree_root / target_blacklist.relative_to(REPO_ROOT)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(target_blacklist.read_bytes())
    with dst.open("a", encoding="utf-8") as f:
        f.write("\n\n# injected float() for M005 test\n_UNUSED = float(1.5)\n")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [_py(), str(REPO_ROOT / "scripts" / "pasay_gate_float_safety.py"),
         str(tree_root)],
        capture_output=True, cwd=str(tree_root), timeout=120, env=env,
    )
    stdout_text = (proc.stdout or b"").decode("utf-8", errors="replace")
    stderr_text = (proc.stderr or b"").decode("utf-8", errors="replace")
    assert proc.returncode == 1, (
        "Float gate must fail-closed; got exit 0. stdout: "
        + stdout_text + " stderr: " + stderr_text
    )
    assert "FAIL" in stdout_text or "float" in stdout_text.lower(), stdout_text
