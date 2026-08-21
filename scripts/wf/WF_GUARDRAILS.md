# WF Guardrails — HISTORICAL / LEGACY

> **STATUS: RETIRED**
> 本文件记录 Pasay 历史 ND workflow 阶段的程序化护栏。
> 已于 `PASAY-SOLO-TRANSITION-001` 正式退役。
>
> 新的项目级工程护栏与原则见 `project_rules.md`。
>
> 历史摘要（仅作审计记录，不再强制）：
> - 17.1 外部平台真实语义固化到测试替身
> - 17.2 用户可见路径禁止静默吞异常
> - 17.3 READY_FOR_OWNER_UX_RETEST 复合门
> - 17.4 命令 timeout 强制化
>
> 上述护栏的精神（业务真相优先、确定性测试、用户错误可见、防止无限等待）仍被
> `project_rules.md` 的"Business truth first"与"确定性 > 智能"原则继承。
> 但 scripts/wf/wf_guardrails.py / wf006_tests.py 对应的程序化强制不再是启动前置条件。
