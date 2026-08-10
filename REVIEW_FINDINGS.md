## REVIEW_FINDINGS

二次安全/代码审查（Phase 5，只读）。审查对象：`pasay-telegram-bot/` 全部新增代码（父仓库中为 untracked，即相对 main 的完整新增面），
并对照后端 `app/api/routers/income.py`、`app/api/deps.py` 核验 API 语义假设。范围：简报 19+7 项排查清单。
方法：全量通读源码 + 82 项测试审阅 + 实测复现关键缺陷（未修改任何代码，未 commit，未部署）。

### 总表（CRITICAL → LOW）

| # | Level | Area | 位置(文件:行) | 问题 | 建议 |
|---|-------|------|---------------|------|------|
| F1 | CRITICAL | 重复入账（Item 4/5） | `pasay_bot/handlers/callback.py:308-328,354-361`（另见 `tests/test_handlers.py` 仅覆盖 confirm 超时） | **create 已写入但响应超时 → 重试产生第二笔收入**。`POST /incomes` 超时时 `income is None`，仅 `guard.fail(key)`，resource 未记录；重试走 resume 发现 `existing_id=""` 再调 `create_income`。已实测复现：第 1 次点击后端已落 1 条 pending、响应超时；同卡重试后共 2 条收入（1 pending 滞留 + 1 confirmed）。后端无幂等键/唯一约束可兜底。`test_backend_timeout_after_write` 只模拟了 confirm 端点"写后超时"，未覆盖 create 端点。 | 必须修。方案 A：create 超时后用 (lease_id, received_date, amount, method) 做 GET 对账，命中则复用并 settle；方案 B：后端 `POST /incomes` 支持 Idempotency-Key 或对 (lease_id, received_date, amount) 加约束；方案 C（最低限度）：resource 未知时禁止同卡自动重试，改为提示人工核对 /finance。并补 create-timeout-after-write 回归测试。 |
| F2 | HIGH | 权限/功能（Item 13、额外自查·config） | `pasay_bot/main.py:74`、`pasay_bot/config.py:12`；后端 `app/api/routers/income.py:144-147` | **冲销（reverse）实际不可用或后端兜底被降级**。bot 只用 `pasay_api_key` 建了一个 client，`pasay_admin_api_key` 全代码零使用（仅 config 定义）。后端 `reverse` 是 `admin_only`：若 key 为 manager → OWNER 冲销必 403；若把 admin key 填进 `PASSAY_API_KEY` 硬跑 → 后端对 create/confirm 的"最终 enforcement"降级为 admin 全权，SECRETARY 越权仅靠 bot 层角色表。`.env.example` 注释声称"admin key only used for OWNER reverse ops"但代码未实现。 | 必须修。按操作切换 key（create/confirm 用 manager key、reverse 用 admin key），或后端给 manager 开放 reverse 并保持 bot 单一 key；同时删除死配置或补测试断言 admin key 被使用。 |
| F3 | MEDIUM | 并发/崩溃恢复（Item 6） | `pasay_bot/state/store.py:38,162`、`pasay_bot/handlers/callback.py:280-289` | **in_flight 无老化，进程中断后锁死 7 天**。`acquire` 插入 in_flight 后若进程在 settle 前被杀/重启，该 nonce 卡 7 天（`DEFAULT_IDEMPOTENCY_TTL`），之后每次点击都回"处理中"；若中断前 create 实际已落库，用户换新卡重录会产生第二笔。单进程内 `RLock`+无 await 使 acquire 原子，但跨重启无恢复。 | 建议修。in_flight 加短老化（如 60-120s 后按 failed 处理并允许重试）；或启动时把遗留 in_flight 批量转 failed；并为"写后崩溃→重启→同卡重试"补测试。 |
| F4 | MEDIUM | 越权/信息泄漏（Item 7、额外自查·group） | `pasay_bot/handlers/commands.py:26-70`、`pasay_bot/handlers/nl_bridge.py:41-66` | **读路径完全无权限门禁**。`/properties` `/finance` `/overdue` `/rent` 及菜单/导航对任何 Telegram 用户（含群内陌生人）开放；`PERMISSION_READ` 定义了但零使用。任何人知道 bot 用户名即可拉取财务汇总、逾期清单、租客姓名与地址（电话未渲染，风险略低）。写路径有门禁，读路径没有。 | 建议修。读命令按 `PERMISSION_READ` 校验（OWNER/SECRETARY 放行，其余拒绝），或至少在群聊中拒绝未知用户读操作；补充未知用户调 /finance 的测试。 |
| F5 | MEDIUM | 功能缺口/漏记（Item 1/11/12） | `pasay_bot/handlers/callback.py:576-586,588-609` | **SECRETARY 登记的 pending 收入在 bot 内无确认入口**。`_render_pending_card` 的确认按钮只对"当前点击者"渲染；SECRETARY 无 confirm 权限 → 卡片无按钮；OWNER 也没有任何 pending 列表可触发 `cnf:inc`。后果：SECRETARY 录入的账永远 pending，不计入已收，逾期报表继续显示应缴 → 现实中有重复催收/漏记风险。 | 建议修。增加 pending 列表入口（如 /pending 或逾期卡上对 pending 收入给出 OWNER 可点的确认按钮），或明确依赖后端面板确认并在 README 声明。 |
| F6 | MEDIUM | 群聊 DoS（Item 2/6/15） | `pasay_bot/handlers/callback.py:277-283,292-296` | **guard.acquire 先于会话归属校验，群内可烧 nonce 锁死对方卡片**。同群中 OWNER/SECRETARY（均有 RENT_ENTRY）点击对方 `cnf:ren` 卡：先建全局 `ik:cnf:ren:{nonce}` in_flight，随后 conv 归属校验失败返回"过期"，但该 key 已 in_flight → 真主人再点被"处理中"挡到 7 天。无写入、无金额风险，但可被误触/恶意制造 7 天卡片锁死。 | 建议修。把会话归属（conv nonce 匹配）与权限校验移到 `guard.acquire` 之前；或 idempotency key 加 chat/user 维度。 |
| F7 | LOW | 误导性提示（Item 4/13） | `pasay_bot/handlers/callback.py:532-550` | **reverse 超时对账后若收入仍为 confirmed，toast 显示"✅ 已处理"**。冲销实际未生效但用户被告知已处理（卡片本身仍显示 confirmed+新冲销按钮，可继续操作，无金额错误，但提示有歧义）。 | 可不修，或按 `reverse=True` 时对 confirmed 状态给出"未冲销，请重试"的 toast。 |
| F8 | LOW | HTML 截断（Item 9/19） | `pasay_bot/render/html.py:74-87` | **`H.truncate` 可能从 HTML 标签/实体中间切断**（如 `&amp;` → `&am`、`<b>` 半截），超过 4096 UTF-16 的长页发送时报 `can't parse entities`，消息不送达（非用户可见 traceback）。现有测试只断言 UTF-16 长度，未断言标签闭合。 | 可不修。建议截断后做标签闭合校验或改用纯文本 fallback；补一个长列表 + 含 `&` 名称的测试。 |
| F9 | LOW | 健壮性（Item 19） | `pasay_bot/state/store.py:135-145` | `get_idempotency` 对 `result_json` 直接 `json.loads` 无 try，DB 内容损坏时处理器抛异常（仅本地 state DB 可写，风险很低）。 | 可不修，包一层 try 即可。 |
| F10 | LOW | 数据容错（Item 8） | `pasay_bot/api_client.py:15-21` | `_to_decimal` 对非法值静默转 0：后端返回损坏金额时界面显示 ₱0 而非报错，可能掩盖数据问题。正常路径全为 Decimal/str，无 float，符合要求。 | 可不修；如需严格可改为显式错误。 |
| F11 | LOW | audit 完整性（Item 12，后端代码） | `app/api/routers/income.py:125-137,152-162` | 后端 `confirm`/`reverse` 的 audit `old_value=serialize_row(obj)` 在变更后取值，old_value 实际记录的是新状态（`old` 变量算而未用）→ 审计无法还原变更前状态。bot 侧所有写均走后端 confirm/reverse，audit 行存在，路径无缺失；此条为审查中发现的后端缺陷（超出 bot 范围，仅提示）。 | 建议后端修复为 `old_value=old`。 |

### 19+7 排查清单逐项结论

| # | 项 | 结论 |
|---|----|------|
| 1 | 重复入账（双击 confirm） | 基本通过：nonce+idempotency（in_flight/done/failed）+ 后端 409 兜底，双击只写一次；例外见 F1（create 超时重试）、F3（崩溃后换卡重录）。 |
| 2 | 重复 callback / replay | 通过：done 重放不触 API；过期/坏数据被拒；例外见 F6（群内烧 nonce）。 |
| 3 | Telegram retry | 通过：callback 不重投，双击为两个 update，guard 串行原子；PTB 默认 sequential。 |
| 4 | API timeout | confirm/reverse 超时对账正确（不误报"未修改"）；**create 超时对账缺失 → F1（CRITICAL）**。 |
| 5 | DB 事务一致性 | bot 无 DB 写；create→confirm 两步断链：id 已知时 resume 正确，id 未知时（F1）断链；后端 confirm/reverse 单事务 + audit。 |
| 6 | 并发 / race | 单进程内 acquire 原子（RLock+无 await），in_flight 阻塞有效；跨进程/崩溃无保护 → F3。后端 confirm TOCTOU 影响仅状态覆盖，无金额重复。 |
| 7 | permission bypass | 写路径：手工构造 `cnf:inc`/`rv` 对 SECRETARY/未知用户被 bot 层拒绝（有测试）；后端 403 依赖 key 角色 → F2 使兜底打折；读路径无门禁 → F4。 |
| 8 | float 金额 | 通过：全 Decimal/str，`create_income` 传 `str(Decimal)`，无 float；见 F10 容错 nit。 |
| 9 | HTML injection | 通过：文本全走 `H.escape`（含属性/地址/租客名），按钮文本为纯文本无解析；例外见 F8（截断可拆标签，属可用性非注入）。 |
| 10 | callback_data 泄露 | 通过：仅 action/entity/ref(数字 id)/nonce/ts，无电话/PII；≤64B 有断言。 |
| 11 | stale button | 通过：`_expired` 用 ts+TTL 先行拦截（15 分钟）；bot 无账期概念，"换账期"由 TTL 覆盖。 |
| 12 | audit log 缺失 | bot 侧通过：所有财务写均走后端 confirm/reverse；无旁路写路径；后端 audit old_value 缺陷见 F11。 |
| 13 | reverse 异常 | 409/404/500 处理正确、失败可重试、超时对账正确；**key 角色不匹配 → F2（HIGH）**；toast 歧义见 F7。 |
| 14 | pagination bug | 通过：decode 限制 ref 为数字，页数 clamp 到 [1,total_pages]，切片安全；负/超大页被规范化。 |
| 15 | 状态机漏洞 | 通过：amount→date→method→confirm 转移完整；cancel/过期/非法 state 均回落 NL；文本输入不触发写；例外见 F6（归属校验顺序）。 |
| 16 | 直接 DB 写 | 通过：无任何绕过 API 的写；SQLite 仅为本地会话/幂等状态。 |
| 17 | LLM 幻觉 | 通过：Native Bot 完全确定性，Hermes NLU 未接线；金额/状态不受 LLM 影响。 |
| 18 | i18n key 缺失 | 通过：zh/en 键集合一致（脚本比对无缺失）；`t()` 缺键回退显示裸 key 而非 KeyError。 |
| 19 | 日志/错误处理 | 通过：用户端无 traceback/SQL/JSON 裸错误；错误均有 i18n 文案；`main.py` 异常打印不含 token（httpx/PTB 错误串不含 URL/token，已实测）。 |
| 20 | 未知 telegram_user_id | 通过：`role_for_telegram_id(None/未知)→None`，`has_permission(None)=False`，写被拒（有测试）。 |
| 21 | group 场景 | 写防串话通过：(chat_id,user_id) 主键 + conv nonce 绑定；群内非 OWNER/SECRETARY 无法触发写；读暴露见 F4，nonce 烧毁见 F6。 |
| 22 | timeout 判定 | 通过：`httpx.TimeoutException→PasayApiTimeoutError`（不确定性），500/4xx→PasayApiError（确定性），区分可靠。 |
| 23 | config secret | 通过：无 secret 打印；`.env` 权限 600 且被 gitignore；`.env.example` 全占位符；admin key 死配置见 F2。 |
| 24 | --dry-run / 启动脚本 | 通过：只打印 getMe username/id 与 STATE_DB 路径；不打印 token；token 进日志风险低（错误串不含 token）。 |
| 25 | 测试真实性 | 大部分真实（82 项通过，断言有效）；缺口：create-timeout-after-write 未覆盖（F1 已实测失败）、in_flight 崩溃恢复未覆盖（F3）、HTML 截断标签闭合未覆盖（F8）。 |

### 结论

- **必须修（1 CRITICAL + 1 HIGH）**：F1 是唯一会直接产生"重复入账"的路径，且正是简报 Item 4 明令禁止的场景——修法与回归测试方案已给出；F2 使 OWNER 冲销功能不可用或后端权限兜底失效——按操作切换 admin/manager key 或调整后端角色。
- **建议修（4 MEDIUM）**：F3（in_flight 老化，防崩溃锁死+二次入账）、F4（读路径加权限门禁，堵越权信息泄漏）、F5（给 SECRETARY 录入的 pending 提供 OWNER 确认入口）、F6（归属校验前移，防群聊烧 nonce）。这些不直接产生金额错误，但影响正确入账闭环与权限边界。
- **可不修（5 LOW）**：F7-F11 为提示歧义、截断边界、本地 DB 健壮性与后端 audit 字段缺陷，无金额/越权直接影响。
- **复核要点**：修复 F1/F2 后应回归全部 82 项测试并新增 F1 复现用例（create 写后超时→重试仅 1 笔）、F3 重启恢复用例；建议修复后再上线。

### 简短总结

- 发现 **11 项**：CRITICAL 1、HIGH 1、MEDIUM 4、LOW 5。
- 核心风险：**create 超时后重试会重复入账（已实测复现）**、**reverse 因 admin key 未接线而不可用/兜底降级**。
- 19+7 清单中写路径幂等、HTML 转义、金额 Decimal、callback 数据、状态机、i18n、无直接 DB 写、无 LLM 依赖等 14 项通过；5 项存在缺陷（对应 F1-F6）。
- **建议：修复 CRITICAL/HIGH 后再回归测试并部署**；MEDIUM 可随下一迭代处理。

---

## Phase 5b 修复记录（按 PHASE5B_FIX_BRIEF.md 执行，2026-08-10）

> 状态：**F1–F9、F11 已修复并回归**；F10（金额静默归零）与日志/错误处理按简报保持现状，本轮不处理。
> 验证：bot `env -u PYTHONPATH .venv/bin/python -m pytest tests` **99 passed**（原 82 + 新增 17）；
> 后端 `.venv/bin/python -m pytest tests` **102 passed**（未破坏既有 102 项，未做 migration，未 commit/部署/launchd）。

| # | 处理方式 | 新增测试 / 验证 |
|---|---------|-----------------|
| F1 🔧 | create 超时后按 `(lease_id, received_date, amount, method)` 调 `GET /incomes` 对账（`api_client.find_income`），命中即复用+settle+补记 resource，绝不再次 create；并在每次 create **之前**做同一对账，覆盖"换新卡/新 nonce 重录同单元同账期"的重复场景；同时修正 `test_backend_timeout_after_write` 改走 create 端点。 | `test_create_timeout_after_write_no_duplicate`、`test_new_card_re_records_same_period_reuses_pending`（重录同账期仅 1 笔）、`test_backend_timeout_after_write`（改为 create 写后超时路径）。 |
| F2 🔧 | bot 双 key：`PASSAY_API_KEY`(manager) 客户端做 create/confirm，`PASSAY_ADMIN_API_KEY`(admin) 客户端专做 reverse；`build_application` 注入 `admin_api_client`，`_handle_reverse` 走 admin client；未配置 admin key 时 OWNER 的 reverse 按钮不渲染、手工回调被拒并记日志。 | `test_reverse_uses_admin_key_writes_use_manager_key`（断言 reverse=Bearer admin-key、create/confirm=Bearer manager-key）、`test_reverse_disabled_without_admin_key`（无 admin key → 按钮隐藏 + 回调拒绝）。 |
| F3 🔧 | `idempotency_keys.in_flight` 增加 120s 短老化（`DEFAULT_IN_FLIGHT_TTL`）：`acquire` 遇过期 in_flight 转 failed 返回 retry；`StateStore.migrate()` 启动时把遗留 in_flight 批量转 failed。 | `test_stale_in_flight_allows_retry`、`test_recover_stale_in_flight_on_startup`、端到端 `test_crash_after_write_restart_no_duplicate`（写后中断→重启→同卡重试仍仅 1 笔）。 |
| F4 🔧 | 新增 `roles.has_read_permission`（`PERMISSION_READ` 子集校验）；`/properties` `/finance` `/overdue` `/rent`、菜单/导航回调（nav/page/rent/detail）、NL 路由全部按读权限门禁，未知用户/群内陌生人拒绝。 | `test_unknown_user_read_refused`（/finance 命令+nav 回调均被拒、无 API 调用）、`test_owner_and_secretary_can_read`。 |
| F5 🔧 | 新增 `/pending` 命令：列出 pending 收入（租客/物业/Unit/金额/日期/方式），OWNER（有 `RENT_CONFIRM`）每行可点 `cnf:inc <id>` 一键确认；SECRETARY 可查看但无确认按钮。 | `test_owner_confirms_secretary_pending_via_pending_command`、`test_secretary_pending_list_has_no_confirm_buttons`。 |
| F6 🔧 | `_confirm_rent_entry` 把会话归属校验（conv 存在 + state=rent_confirm + nonce 匹配）与权限校验移到 `guard.acquire` **之前**，群内他人点击不再先烧 in_flight。 | `test_group_other_user_click_does_not_lock_card`（他人先点不产生 idempotency key，OWNER 随后点击正常入账）。 |
| F7 🔧 | reverse 超时对账后若仍 confirmed：toast 改为「⚠️ 未冲销，请重试。」（`rent.reverse_failed_toast`），不再显示「已处理」。 | `test_reverse_timeout_reconcile_still_confirmed_toast`。 |
| F8 🔧 | `H.truncate` 截断后做标签闭合/实体安全校验：不在 `<tag`/`&entity` 中间截断，并校验标签配对；不满足时回退为纯文本（去标签+重转义），确保 Telegram 不会收到半开标签/悬空实体。 | `test_truncate_never_splits_entity_or_tag`、`test_truncate_long_list_with_ampersand_names`（60 条含 `&` 名称的房源长列表）。 |
| F9 🔧 | `store.get_idempotency` 的 `json.loads` 包 try，损坏 `result_json` 返回 `result=None` 不抛异常。 | `test_corrupt_result_json_returns_none_result`。 |
| F11 🔧 | 后端 `app/api/routers/income.py`：confirm/reverse 审计 `old_value=serialize_row(obj)`（变更后取值）改为 `old_value=old`（变更前快照），恢复可还原变更前状态；只改这两处。 | 后端 102 项全绿（含 `test_audit.py`/`test_financial.py` 既有断言）。 |

---

## Phase 5c 最终确认（2026-08-10，只读审查）

方法：通读当前源码 + 完整 diff（`pasay-telegram-bot/` 新增面 + `app/api/routers/income.py` F11 两行改动），逐项复核 F1-F9/F11 修复与回归；未修改任何代码、未 commit、未部署。

### 通过项

| # | 复核点 | 结论 |
|---|--------|------|
| F1 | create 前对账只在「无已知 resource」时走：`_confirm_rent_entry` 先走 `guard.resource(key)` resume 分支，resource 为空才调 `api_client.find_income`；命中即复用+settle+补记 resource，绝不二次 create；换新卡/新 nonce 重录同账期同样先对账再创建。`test_create_timeout_after_write_no_duplicate` 为真实断言（create 落库 pending 后响应超时 → GET /incomes 对账 → confirm 复用，仅 1 笔；同卡二击纯 replay、零新增 API 调用）；`test_new_card_re_records_same_period_reuses_pending` 覆盖新 nonce 场景。 | 通过 |
| F2 | `build_application` 注入双 client：create/confirm 走 `PASSAY_API_KEY`(manager) 的 `api_client`，reverse 走 `PASSAY_ADMIN_API_KEY`(admin) 的 `admin_api_client`；无 admin key 时 `_can_reverse` 不渲染冲销按钮、`_handle_reverse` 拒绝手工回调并记日志。`test_reverse_uses_admin_key_writes_use_manager_key` 断言 create/confirm=Bearer manager-key、reverse=Bearer admin-key；`test_reverse_disabled_without_admin_key` 覆盖按钮隐藏+回调拒绝。 | 通过 |
| F3 | `acquire` 中 `_is_stale_in_flight` 对超 120s 的 in_flight 转 failed 返回 retry（保留 resource 供 resume）；`migrate` 启动时 `recover_stale_in_flight` 仅更新 `status='in_flight'` 的过期行，不误伤 done。`test_stale_in_flight_allows_retry`、`test_recover_stale_in_flight_on_startup` 真实；端到端 `test_crash_after_write_restart_no_duplicate` 复现「写后崩溃→重启→同卡重试」且 0 次重复 create。 | 通过 |
| F4 | `/properties` `/finance` `/overdue` `/rent`、菜单/导航回调（nav/page/rent/detail）与 NL 路由均先过 `has_read_permission`（OWNER/SECRETARY 放行、未知用户拒绝且零 API 调用）。`test_unknown_user_read_refused`（命令+nav 回调均拒）、`test_owner_and_secretary_can_read` 真实。 | 通过 |
| F5 | `/pending` 命令存在：OWNER 每行可点 `cnf:inc <id>` 一键确认（`test_owner_confirms_secretary_pending_via_pending_command`）；SECRETARY 可查看但无确认按钮（`test_secretary_pending_list_has_no_confirm_buttons`）。 | 通过 |
| F6 | 归属校验（conv 存在 + state=rent_confirm + nonce 匹配）与权限校验均在 `guard.acquire` 之前；`test_group_other_user_click_does_not_lock_card` 验证他人先点不产生 idempotency key，OWNER 随后点击正常入账。 | 通过 |
| F7 | reverse 超时对账后仍 confirmed：toast 为「⚠️ 未冲销，请重试。」（`rent.reverse_failed_toast`），不再显示「已处理」；`test_reverse_timeout_reconcile_still_confirmed_toast` 断言「未冲销」且不含「已处理」。 | 通过 |
| F8 | `H.truncate`：`_cut_is_safe` 校验截断点不在 `<tag`/`&entity` 中间并做标签配对，不满足则纯文本回退（去标签+重转义）并逐字回退至安全边界；`test_truncate_never_splits_entity_or_tag`、`test_truncate_long_list_with_ampersand_names` 真实。 | 通过 |
| F9 | `store.get_idempotency` 对损坏 `result_json` 包 try，返回 `result=None` 不抛异常；`test_corrupt_result_json_returns_none_result` 真实。 | 通过 |
| F11 | 后端 `app/api/routers/income.py` 仅 2 行改动：confirm/reverse 审计 `old_value=serialize_row(obj)`（变更后）→ `old_value=old`（变更前快照），恢复可还原变更前状态；确认 `old` 均在状态变更前捕获，未触碰其他逻辑。 | 通过 |
| 回归 | bot `env -u PYTHONPATH .venv/bin/python -m pytest tests` → **99 passed**；后端 `.venv/bin/python -m pytest tests` → **102 passed**。 | 通过 |

### 结论

- **无残留 CRITICAL/HIGH**：F1-F9/F11 修复实现正确，未发现新的 重复入账/越权/注入/金额错误 路径；两套全量回归全绿，可进入部署。
- 不阻塞的 LOW（非本轮回归，供后续迭代参考）：
  1. `api_client.find_income` 不区分 income 状态：对账命中 reversed/confirmed 记录时也会复用。极端场景（同日同额同方式第二次付款、或冲销后重录同参数）会被抑制为新录入（漏记而非重复）。建议对「新 nonce + 命中非 pending」在 toast 提示已存在（含 income_id），或跳过 reversed 命中。
  2. `_reconcile_create_after_timeout` 内 `confirm_income` 若再次超时无内层 catch（异常上抛、key 停留 in_flight 至 120s 老化、无 toast）；因重试仍走对账复用，不会产生重复入账。
- F10（金额静默归零）与日志/错误处理按 PHASE5B_FIX_BRIEF 明确不在本轮范围，维持现状。

### 简短总结

F1-F9/F11 修复经源码逐行复核 + 全量回归（bot 99 / 后端 102）确认通过，无 CRITICAL/HIGH 残留；仅余 2 项 LOW 边角建议，不阻塞部署。

---

## F12 修复记录（按 /tmp/F12_BRIEF.md 执行，2026-08-10）

> 状态：**F12 已修复并回归**；未 commit、未重启 bot（重启由 orchestrator 协调）。
> 现象：`_edit()` 调 `edit_message_text` 时，若新内容与当前消息完全一致（如重复点某 unit 的「登记收租」），Telegram 返回 `BadRequest: Message is not modified`，每次点击抛一次栈级错误并刷 err.log。

### 修复方式

| 项 | 处理 |
|----|------|
| 新增 helper | `pasay_bot/handlers/edit_utils.py`：`edit_message_text_idempotent()` 包一层 `edit_message_text`，仅当错误信息含 `"Message is not modified"` 时视为「内容未变」幂等忽略（不抛、不刷日志），其余 `BadRequest` 原样抛出。 |
| `_edit` | `callback.py` 的 `_edit()`（L83-92）改走该 helper，覆盖全部 `_edit` 调用点（登记收租/确认卡/冲销卡/超时兜底/取消等）。 |
| 其他 edit 点 | `commands.py` 的 `show_unit_page` 与 `show_rent_units`（error 分支 + 成功分支）共 3 处 `edit_message_text` 一并改走 helper，仓库内不再有绕过 helper 的 `edit_message_text` 调用。 |

### 测试

| 测试 | 断言 |
|------|------|
| `test_edit_noop_message_not_modified`（新增，F12 核心） | 用 `_NoopEditBot`（fake bot 的 `edit_message_text` 在内容与上次一致时抛 `BadRequest("Message is not modified")`）模拟重复点「登记收租」（`rn:go:1` 连点两次）：不抛异常、两次渲染同一文本、`caplog` 无该错误的 ERROR 日志（不再刷 err.log）、会话状态保持 `rent_amount`。已实测：临时去掉容错后本测试失败，恢复后通过。 |
| `test_edit_other_bad_request_still_raises`（新增） | 非 "Message is not modified" 的 `BadRequest`（如 `message can't be edited`）仍从 `_edit` 正常抛出。 |
| `conftest.py` | `make_app` 增加可选 `bot=` 参数以便注入上述 fake bot（向后兼容，默认 `FakeBot()`）。 |

### 回归

- dev（`pasay-pm/pasay-telegram-bot`）：`env -u PYTHONPATH <bot venv>/bin/python -m pytest tests` → **101 passed**（原 99 全绿 + 新增 2）。
- /opt 部署副本（`/opt/pasay-pm/pasay-telegram-bot`，自带 `.venv`）：**101 passed**，与 dev 逐文件 diff 一致。
- 同步文件：`pasay_bot/handlers/edit_utils.py`（新增）、`callback.py`、`commands.py`、`tests/conftest.py`、`tests/test_handlers.py`。
- 未 commit、未重启 bot、未改动后端与 i18n 既有内容。
