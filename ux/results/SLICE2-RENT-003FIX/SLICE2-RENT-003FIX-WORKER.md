# SLICE2-RENT-003FIX — Worker Result（CODE_READY 阶段，未提交）

## status

`REVIEW_READY`（实现 + Level 1/2 测试通过；提交步骤按 envelope 禁令跳过，见 unresolved）

## task

- task_id: `SLICE2-RENT-003FIX-WORKER`
- branch: `feature/telegram-ui-v2`
- baseline HEAD: `ddc17c78`（`ddc17c7` short）
- mode: `WORKSPACE_WRITE`

## objective

修复 V1.3 Slice 2 Rent Status / Payment Match 的单元号匹配误判：

- `"608"` 不得命中 `"1608"` / `"DEV-BAY-1608"`（digit 后缀假阳性，Owner 会看到别的房源付款/状态）
- `"1608"` 应能命中前缀式房源号 `"DEV-BAY-1608"`（dev seed 风格）
- `nl_bridge._answer_unit_status` 由 exact-only 改为归一化匹配，并支持 0 / 1 / 多命中（多命中只读候选卡，不自动选、不写）
- 句尾标点（`1608?` / `1608.`）不掩盖单元号
- 租客名匹配保持词边界（`John` 不命中无关的 `DEV Paolo Cruz`）

## files_changed

- `app/services/payment_match.py` — `_unit_matches`：后缀匹配增加非数字边界校验
- `pasay-telegram-bot/pasay_bot/handlers/nl_bridge.py` —
  - `_UNIT_TOKEN` 支持前缀式单元号（`DEV-BAY-1608`）
  - 新增 `_unit_number_matches`（与 `payment_match._unit_matches` 同规则）
  - 新增共享 `_status_candidates` / `_send_status_answer`（unit/tenant 两路径复用）
  - `_answer_unit_status`：0 命中 → `no_unit`；1 命中 → `rent_status_card`；多命中 → 只读 `tenant_candidates_card`
- `pasay-telegram-bot/tests/test_rent_status_nl.py` — 新增匹配/歧义/词边界回归测试
- `tests/test_payment_match.py` — 新增 `_unit_matches` / `parse_hints` 回归测试
- 本报告文件

## tests_run / tests_passed

| suite | command | result |
|---|---|---|
| bot 全量 | `pytest pasay-telegram-bot/tests` | 237 passed |
| 后端 matcher（纯逻辑） | `pytest tests/test_payment_match.py -k "not match_endpoint"` | 18 passed |
| 语法编译 | `py_compile` 4 个改动文件 | OK |

## failures

- `test_match_endpoint_exact_high` / `test_match_endpoint_duplicate`：环境受限（本工作区无 PostgreSQL 测试库，sqlalchemy OperationalError）。与本改动无关，原测试即依赖测试库。

## risks

- `_UNIT_TOKEN` 扩展会捕获带连字符前缀的单元号（如 `DEV-BAY-1608`）；检测器先于租客名匹配，行为与现有 bot 测试一致（全量 237 通过）。
- 同 unit 存在多条 active lease 时，unit 查询改走候选卡而非取第一条（只读、不自动选，语义更安全）。
- 边界规则把 `"8"→"1608"`、`"20"→"1020"`、`"11608"→"1608"` 等数字粘连后缀判为不匹配；这是本卡修复目标。

## unresolved

- **提交（`git add`/`commit`）未执行**：envelope 明确禁止 git add/commit/merge/push；按规则「review 后决定提交」。当前改动已落盘于工作区（4 个源文件 + 本报告），待 Bridge/Fugui 走 CODE_READY Gate 提交（message 前缀 `SLICE2-RENT-003FIX:`）。
- 2 个 PostgreSQL 依赖的 endpoint 测试未跑（环境无测试库）。

## diff / artifact

- 工作区 diff：`git diff`（4 个改动文件，289 insertions / 97 deletions 含重构）
- HEAD 未变：`ddc17c78`

## rules

- rules_version: `2026-08-13.2`
- rules_sha256: `b32a58a8721460fcb4bcb6a1820e7e0093edae9832246ab597116a95a412d953`
- preflight: `RULES_PREFLIGHT_OK`
