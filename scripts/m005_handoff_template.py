#!/usr/bin/env python3
"""M005 Milestone C: Final owner handoff report template.

This module exposes :func:`print_handoff`, which composes the canonical
Chinese-language closeout report for Milestone C acceptance.

The function is intentionally PURE: it does NOT read the filesystem,
query databases, or shell out.  Callers pre-compute A/B/C evidence by
running the sister scripts in this directory and then pass plain dicts.

Sections in the rendered report (in order):
    1. 标题 / Issue 与 PR 元信息
    2. base / head / commit chain
    3. Changed files 清单
    4. A 交付内容
    5. B 交付内容
    6. C 交付内容
    7. 22 Routers Scope 矩阵
    8. God View 合约
    9. Pagination 合约
    10. i18n 合约
    11. Float Gate
    12. Envelope Schema 合约
    13. M001–M005 测试命令与结果
    14. Alembic 双向验证
    15. 依赖残留扫描
    16. 技术债 & Owner-only 决策清单
    17. WORKTREE_CLEAN 标志
    18. DEV_HANDOFF_READY 签字位

不执行本脚本；仅定义 print_handoff(...)。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any, Optional


DEFAULT_MATRIX_ROUTERS = [
    "auth", "onboarding", "properties", "property_channel", "units", "tenants",
    "leases", "income", "payments", "expense", "commission", "tasks", "reports",
    "repairs", "attachments", "audit", "operations", "evidence", "viewings",
    "move_out", "deposit_settlements", "telegram_webhook", "internal_ingest",
]


def _h1(title: str) -> str:
    return f"\n# {title}\n"


def _h2(title: str) -> str:
    return f"\n## {title}\n"


def _h3(title: str) -> str:
    return f"\n### {title}\n"


def _kv_table(rows: list[tuple[str, Any]], empty: str = "—") -> str:
    lines = ["| 字段 | 值 |", "|------|----|"]
    for k, v in rows:
        if v is None or v == "":
            v_display = empty
        elif isinstance(v, (list, dict)):
            v_display = "```json\n" + json.dumps(v, ensure_ascii=False, indent=2) + "\n```"
        else:
            v_display = str(v)
        lines.append(f"| {k} | {v_display} |")
    return "\n".join(lines) + "\n"


def _bullet_list(items: list[Any], empty: str = "（待填充）") -> str:
    if not items:
        return f"- {empty}\n"
    out: list[str] = []
    for it in items:
        if isinstance(it, (list, dict)):
            out.append("- " + json.dumps(it, ensure_ascii=False))
        else:
            out.append(f"- {it}")
    return "\n".join(out) + "\n"


def _bool_mark(flag: Optional[bool]) -> str:
    if flag is True:
        return "✅ PASS"
    if flag is False:
        return "❌ FAIL"
    return "⏳ 待执行"


def print_handoff(
    *,
    issue: Optional[dict[str, Any]] = None,
    pr: Optional[dict[str, Any]] = None,
    base_branch: str = "main",
    head_branch: str = "milestone/5-v1-closeout",
    commit_chain: Optional[list[dict[str, Any]]] = None,
    changed_files: Optional[list[str]] = None,
    deliverable_a: Optional[dict[str, Any]] = None,
    deliverable_b: Optional[dict[str, Any]] = None,
    deliverable_c: Optional[dict[str, Any]] = None,
    routers_matrix: Optional[dict[str, Any]] = None,
    god_view_contract: Optional[dict[str, Any]] = None,
    pagination_contract: Optional[dict[str, Any]] = None,
    i18n_contract: Optional[dict[str, Any]] = None,
    float_gate: Optional[dict[str, Any]] = None,
    envelope_schema: Optional[dict[str, Any]] = None,
    m001_m005_tests: Optional[dict[str, Any]] = None,
    alembic_roundtrip: Optional[dict[str, Any]] = None,
    dependency_residue: Optional[dict[str, Any]] = None,
    tech_debt: Optional[list[dict[str, Any]]] = None,
    owner_only: Optional[list[dict[str, Any]]] = None,
    worktree_clean: Optional[bool] = None,
    dev_handoff_ready: Optional[bool] = None,
) -> None:
    """Render the Milestone C Chinese handoff report to stdout."""
    issue = issue or {}
    pr = pr or {}
    commit_chain = commit_chain or []
    changed_files = changed_files or []
    tech_debt = tech_debt or []
    owner_only = owner_only or []

    out: list[str] = []
    out.append(_h1("PASAY-M005 Milestone C 验收交付报告（Owner Handoff）"))
    out.append(f"> 生成时间 (UTC): {datetime.utcnow().isoformat()}Z")
    out.append(f"> 工作区: `{head_branch}`")

    out.append(_h2("1. Issue / PR 元信息"))
    out.append(_kv_table([
        ("Issue #", issue.get("number")),
        ("Issue 标题", issue.get("title")),
        ("Issue URL", issue.get("url")),
        ("PR #", pr.get("number")),
        ("PR 标题", pr.get("title")),
        ("PR URL", pr.get("url")),
        ("Base Branch", base_branch),
        ("Head Branch", head_branch),
    ]))

    out.append(_h2("2. Commit Chain"))
    if commit_chain:
        lines = ["| # | SHA | Author | Subject | Date |", "|---|-----|--------|---------|------|"]
        for i, c in enumerate(commit_chain, 1):
            sha = str(c.get("sha", ""))[:10]
            lines.append(
                f"| {i} | `{sha}` | {c.get('author','—')} | "
                f"{c.get('subject','—')} | {c.get('date','—')} |"
            )
        out.append("\n".join(lines) + "\n")
    else:
        out.append("_（commit chain 待 git log 注入）_\n")

    out.append(_h2("3. Changed Files"))
    out.append(f"共 {len(changed_files)} 个变更文件：\n")
    if changed_files:
        for f in changed_files:
            out.append(f"- `{f}`")
        out.append("")
    else:
        out.append("_（待 `git diff --name-only` 填充）_\n")

    out.append(_h2("4. A 交付内容"))
    if deliverable_a:
        out.append("```json\n" + json.dumps(deliverable_a, ensure_ascii=False, indent=2) + "\n```\n")
    else:
        out.append("_（A 交付内容待填充）_\n")

    out.append(_h2("5. B 交付内容"))
    if deliverable_b:
        out.append("```json\n" + json.dumps(deliverable_b, ensure_ascii=False, indent=2) + "\n```\n")
    else:
        out.append("_（B 交付内容待填充）_\n")

    out.append(_h2("6. C 交付内容（本 Milestone）"))
    if deliverable_c:
        out.append("```json\n" + json.dumps(deliverable_c, ensure_ascii=False, indent=2) + "\n```\n")
    else:
        out.append(
            "- [ ] scripts/m005_routers_matrix.py — 22 routers scope matrix 脚本\n"
            "- [ ] tests/m005_routers_scope_matrix.md — 矩阵输出\n"
            "- [ ] scripts/m005_run_targeted_suites.py — M001–M005 命令\n"
            "- [ ] scripts/m005_alembic_roundtrip.py — Alembic 双向验证\n"
            "- [ ] scripts/m005_dependency_residue.py — 依赖残留 grep\n"
            "- [ ] scripts/m005_handoff_template.py — 本 handoff 模板\n"
        )

    out.append(_h2("7. 22 Routers Scope 矩阵"))
    if routers_matrix:
        endpoints = routers_matrix.get("endpoints", [])
        router_order = routers_matrix.get("router_order", DEFAULT_MATRIX_ROUTERS)
        out.append(f"矩阵含 {len(router_order)} 个 router module，共 {len(endpoints)} 个 endpoint。\n")
        lines = ["| router_module | endpoint_path | methods | org_scoped | has_cross_org_test |",
                 "|---------------|---------------|---------|------------|--------------------|"]
        for r in endpoints[:400]:
            lines.append(
                f"| `{r.get('router_module','?')}` | `{r.get('endpoint_path','?')}` | "
                f"{r.get('methods','')} | {str(bool(r.get('org_scoped'))).lower()} | "
                f"{str(bool(r.get('has_cross_org_test'))).lower()} |"
            )
        if len(endpoints) > 400:
            lines.append(f"| … | 截断 {len(endpoints) - 400} 行，详见 JSON | | | |")
        out.append("\n".join(lines) + "\n")
    else:
        out.append(f"涉及 router modules（{len(DEFAULT_MATRIX_ROUTERS)} 个）：\n")
        out.append(_bullet_list([f"`{n}`" for n in DEFAULT_MATRIX_ROUTERS]))
        out.append("_（运行 `scripts/m005_routers_matrix.py` 填充完整矩阵）_\n")

    out.append(_h2("8. God View 合约"))
    if god_view_contract:
        out.append(_kv_table([
            ("合约定义", god_view_contract.get("contract_definition")),
            ("覆盖 endpoints 数", god_view_contract.get("endpoints_count")),
            ("单元测试数", god_view_contract.get("unit_tests")),
            ("E2E 测试数", god_view_contract.get("e2e_tests")),
            ("状态", _bool_mark(god_view_contract.get("passed"))),
        ]))
    else:
        out.append("_（God View 合约待填充：owner/secretary/tenant 三角色读权限边界 + tests）_\n")

    out.append(_h2("9. Pagination 合约"))
    if pagination_contract:
        out.append(_kv_table([
            ("合约定义", pagination_contract.get("contract_definition")),
            ("默认 page_size", pagination_contract.get("default_page_size")),
            ("最大 page_size", pagination_contract.get("max_page_size")),
            ("覆盖列表 endpoints", pagination_contract.get("endpoints_count")),
            ("状态", _bool_mark(pagination_contract.get("passed"))),
        ]))
    else:
        out.append("_（Pagination 合约待填充：page/page_size/total/pages 响应结构一致性）_\n")

    out.append(_h2("10. i18n 合约"))
    if i18n_contract:
        out.append(_kv_table([
            ("支持 locales", i18n_contract.get("locales")),
            ("默认 locale", i18n_contract.get("default_locale")),
            ("Accept-Language 解析", i18n_contract.get("header_parsing")),
            ("覆盖 response keys", i18n_contract.get("keys_covered")),
            ("状态", _bool_mark(i18n_contract.get("passed"))),
        ]))
    else:
        out.append("_（i18n 合约待填充：Accept-Language → zh/vi/en fallback + 错误消息 key）_\n")

    out.append(_h2("11. Float Gate"))
    if float_gate:
        out.append(_kv_table([
            ("DB NUMERIC(14,2)", float_gate.get("db_numeric_14_2")),
            ("Python Decimal 强制", float_gate.get("python_decimal")),
            ("float 写入拦截", float_gate.get("float_write_blocked")),
            ("扫描命中 float 残留", float_gate.get("residue_hit_count")),
            ("状态", _bool_mark(float_gate.get("passed"))),
        ]))
    else:
        out.append("_（Float Gate 待填充：禁止 float 财务字段，DB NUMERIC + Python Decimal 强制）_\n")

    out.append(_h2("12. Envelope Schema 合约"))
    if envelope_schema:
        out.append(_kv_table([
            ("Envelope 顶层结构", envelope_schema.get("top_level_structure")),
            ("error code 规范", envelope_schema.get("error_code_spec")),
            ("pagination wrapper", envelope_schema.get("pagination_wrapper")),
            ("Cloudflare Worker 兼容", envelope_schema.get("worker_compat")),
            ("状态", _bool_mark(envelope_schema.get("passed"))),
        ]))
    else:
        out.append("_（Envelope Schema 待填充：{ok,data,error,meta} 统一响应 envelope）_\n")

    out.append(_h2("13. M001–M005 测试命令与计数"))
    if m001_m005_tests:
        suites = m001_m005_tests.get("suites") or m001_m005_tests.get("plan", {}).get("suites", [])
        lines = ["| Milestone | -k 表达式 | collected | passed | failed | skipped | 状态 |",
                 "|-----------|-----------|-----------|--------|--------|---------|------|"]
        for s in suites:
            k = s.get("key", "")
            expr = s.get("k_expr", "")
            col = s.get("collected", "—")
            pas = s.get("passed", "—")
            fail = s.get("failed", "—")
            skp = s.get("skipped", "—")
            ok_flag = None
            if isinstance(fail, int):
                ok_flag = fail == 0
            lines.append(
                f"| {k} | `{expr}` | {col} | {pas} | {fail} | {skp} | {_bool_mark(ok_flag)} |"
            )
        out.append("\n".join(lines) + "\n")
        if "overall" in m001_m005_tests:
            ov = m001_m005_tests["overall"]
            out.append(_kv_table([
                ("Overall collected", ov.get("collected")),
                ("Overall passed", ov.get("passed")),
                ("Overall failed", ov.get("failed")),
                ("Overall skipped", ov.get("skipped")),
                ("Overall errors", ov.get("errors")),
                ("总体状态", _bool_mark(ov.get("failed", -1) == 0 if isinstance(ov.get("failed"), int) else None)),
            ]))
    else:
        out.append(
            "| Milestone | 命令 |\n"
            "|-----------|------|\n"
            "| M001 | `pytest -k 'milestone_1_org_scope_p0 or test_milestone_1' --tb=short` |\n"
            "| M002 | `pytest -k 'rent_closure_m2 or repair_closure_m2' --tb=short` |\n"
            "| M003 | `pytest -k 'm003_expense_scope_hardening or m003_operations_truth_closure' --tb=short` |\n"
            "| M004 | `pytest -k 'm004_' --tb=short` |\n"
            "| M005 | `pytest -k 'm005_ or god_view or paginat or envelope_compat or float_safety or i18n' --tb=short` |\n"
        )
        out.append("\n_（运行 `scripts/m005_run_targeted_suites.py --run --json out.json` 注入计数）_\n")

    out.append(_h2("14. Alembic 双向验证"))
    if alembic_roundtrip:
        out.append(_kv_table([
            ("single_head_ok", _bool_mark(alembic_roundtrip.get("heads", {}).get("single_head_ok"))),
            ("原始 head rev", alembic_roundtrip.get("original_head_rev")),
            ("roundtrip 目标 revs", alembic_roundtrip.get("roundtrip_target_revisions")),
            ("overall_roundtrip_ok", _bool_mark(alembic_roundtrip.get("overall_roundtrip_ok"))),
            ("恢复后 head rev", alembic_roundtrip.get("restored_head_rev")),
        ]))
    else:
        out.append(
            "_（运行 `python scripts/m005_alembic_roundtrip.py` 注入：\n"
            "  1) alembic heads == 1 line\n"
            "  2) 最近 3 revs 的 upgrade→downgrade→upgrade 每步校验）_\n"
        )

    out.append(_h2("15. 依赖残留扫描"))
    if dependency_residue:
        out.append(_kv_table([
            ("扫描目标 tokens", dependency_residue.get("target_tokens")),
            ("总命中数", dependency_residue.get("hit_count")),
            ("命中文件数", dependency_residue.get("files_count")),
            ("每 token 命中", dependency_residue.get("summary_per_token")),
            ("零残留通过", _bool_mark((dependency_residue.get("hit_count") or 0) == 0)),
        ]))
    else:
        out.append(
            "_（运行 `python scripts/m005_dependency_residue.py` 注入；\n"
            "  目标 tokens：Redis / Kafka / Celery / Temporal / Queue2 / Bot2）_\n"
        )

    out.append(_h2("16. 技术债 & Owner-only 决策"))
    out.append(_h3("16.1 技术债（非 Owner-only，SOLO 可后续规划）"))
    if tech_debt:
        lines = ["| # | 模块 | 摘要 | 优先级 | Owner-only |", "|---|------|------|--------|------------|"]
        for i, t in enumerate(tech_debt, 1):
            lines.append(
                f"| {i} | {t.get('module','—')} | {t.get('summary','—')} | "
                f"{t.get('priority','—')} | {str(bool(t.get('owner_only'))).lower()} |"
            )
        out.append("\n".join(lines) + "\n")
    else:
        out.append("_（技术债待收集）_\n")

    out.append(_h3("16.2 Owner-only 决策（SOLO 无权自行决定）"))
    if owner_only:
        lines = ["| # | 决策主题 | 背景 | 选项 | SOLO 建议 |", "|---|----------|------|------|-----------|"]
        for i, o in enumerate(owner_only, 1):
            lines.append(
                f"| {i} | {o.get('topic','—')} | {o.get('context','—')} | "
                f"{o.get('options','—')} | {o.get('recommendation','—')} |"
            )
        out.append("\n".join(lines) + "\n")
    else:
        out.append("_（Owner-only 决策待收集：产品方向/权限边界/冻结架构推翻/财务规则等）_\n")

    out.append(_h2("17. WORKTREE_CLEAN"))
    out.append(f"状态：**{_bool_mark(worktree_clean)}**\n")
    out.append(
        "> 判定标准：`git status --porcelain` 输出为空，\n"
        "> 且 `git diff --cached --stat` 为空，\n"
        "> 且未追踪文件均为 `.env` / 本地日志等已在 `.gitignore` 内。\n"
    )

    out.append(_h2("18. DEV_HANDOFF_READY 签字位"))
    out.append(f"SOLO 交付状态：**{_bool_mark(dev_handoff_ready)}**\n")
    out.append(
        "Owner 签收 checklist（打勾即视为接受）：\n"
        "- [ ] A/B/C 交付内容与 Issue 范围一致\n"
        "- [ ] 22 routers scope matrix 已审阅且 org_scoped 标识无误\n"
        "- [ ] M001–M005 测试在 CI 上全部 green\n"
        "- [ ] Alembic 双向验证通过（throwaway DB 上）\n"
        "- [ ] 依赖残留扫描：Redis/Kafka/Celery/Temporal/Queue2/Bot2 命中数 = 0\n"
        "- [ ] God View / Pagination / i18n / Float Gate / Envelope Schema 五合约均已签字\n"
        "- [ ] 技术债条目已分配后续 Milestone\n"
        "- [ ] Owner-only 决策已做决定或明确延后\n"
        "- [ ] WORKTREE_CLEAN = PASS\n"
        "- [ ] 最终确认 Merge PR 至 authority 分支（Owner 点击）\n"
    )

    sys.stdout.write("\n".join(out))
    sys.stdout.write("\n")


if __name__ == "__main__":
    print_handoff()
