# PASay Telegram Bot — Second Review (Phase D)

Reviewer role: Senior Product Engineer + UX Reviewer + Security reviewer
(via Codex Max self-review on branch `feature/telegram-ui-v2`).
Date: 2026-08-10. All findings verified against the running test suite
(bot 132 passed / backend 102 passed) plus the no-network harness.

## 14 UX questions — walked end-to-end

1. **首次用户要学习吗?** 否。`/start` 直接是带数据的「今日管理中心」，所有操作是
   按钮；`help.text` 已弱化命令、强调按钮（`pasay_bot/render/i18n.py`）。
2. **要记命令吗?** 否。命令仅作兼容（/start /help），主流程全按钮。
3. **首页 5 秒看懂吗?** 是。第一屏 = 本月租金(应收/已收/未收) + 今日待处理 +
   空置数 + 4 个动作按钮，无解释性长文（`commands.py:show_dashboard`）。
4. **高频任务超过 3 击吗?** 否。收租 = 首页→选未付款 Unit→确认 = 3 击
   （原 9 击，见下方统计）。待处理 = 首页→待处理 = 2 击。
5. **有可自动却让用户选的吗?** 已修复。账期=当月、日期=今天、金额=当前应收、
   方式=最近一次使用（`state/store.py:user_defaults`），最终确认不可跳过。
6. **按钮太多吗?** 首页 4 个主按钮；收集列表每行 1 个 Unit 按钮；逾期页每行
   2 个（登记+详情）+ 首页。无单页 >12 按钮。
7. **有技术术语吗?** 已清理：「Transaction confirmed successfully」→
   「✅ 收租成功」；「Callback expired」→「⚠️ 这个操作已经过期」；
   「无效操作」与「过期」拆分（`i18n.py`）。
8. **有死胡同吗?** 无。所有错误/过期/空状态都有 `[🏠首页]` 或 `[🔄重试]`；
   取消后给首页；`test_back_button_every_page`、`test_expired_state_home_button`、
   `test_api_error_retry_button` 覆盖。
9. **有错误无法恢复吗?** 无。加载失败→重试+首页；财务写失败→同 nonce 重试
   （`retry_confirm_keyboard`），超时写→自动 reconcile 最终状态。
10. **有刷屏吗?** 无。导航全 edit（`test_edit_navigation_no_message_spam`）；
    财务写成功才允许独立消息（成功卡片本身上屏，符合审计友好）。
11. **有重复信息吗?** dashboard 与 finance 页重复展示本月租金，但 dashboard 是
   摘要、finance 是明细（含收支），职责不同；逾期在 dashboard 只显示计数，
   明细在待处理/逾期页。可接受。
12. **已完成状态还显示错误按钮吗?** 否。已付→`[💰查看付款]`、空置/无租约→无
    收租按钮、已撤销→`[🔄重新登记]`（`keyboards.py:unit_page_keyboard` +
    `commands.py:build_unit_page`）；旧卡片过期→过期卡+首页。
13. **用户重复输入已知信息吗?** 否。金额/日期/账期/方式全部自动带出；仅在
    `[✏️修改]` 时需要输入，且有 `[📅今天]` 快捷按钮。
14. **「工程师觉得合理普通人觉得复杂」处?** 复核后仍有两处需要留意（见剩余
    问题 R1/R2），本轮已把最重的 9 步流程压到 3 步，术语清零。

## Security / robustness review

| 项目 | 结论 | 证据 |
| --- | --- | --- |
| 双击幂等 | ✅ 一次写入 | `ik:cnf:ren:{nonce}` in_flight/done/failed；`test_double_click_still_idempotent` |
| timeout-before-write | ✅ 允许重试且不重复 | `test_backend_timeout_before_write` |
| timeout-after-write | ✅ reconcile 复用已落盘 income，绝不二次创建 | `test_backend_timeout_after_write`、`test_new_card_re_records_same_period_reuses_pending` |
| 重复 create | ✅ find_income 先对账 | `_confirm_rent_entry` + F1 测试 |
| stale callback | ✅ 过期卡片 + 首页，无写入 | `test_expired_state_home_button`、`test_expired_callback` |
| 手改 callback_data 越权 | ✅ SECRETARY/未知用户 confirm 被拒；后端 403 兜底 | `test_permission_bypass` |
| 空数据 | ✅ 正面文案 + 首页 | `test_empty_overdue_state`、`test_empty_property_state` |
| 长文案 | ✅ ≤4096 UTF-16 截断且不拆 tag/entity | `test_message_length`、`test_truncate_never_splits_entity_or_tag` |
| 金额 0 / 0.01 / 大额 / 负数 | ✅ PHP 原币、千分位、无 0 尾巴 | `test_money_edge_cases`、`test_reverse_display`、`test_large_amount` |
| HTML 转义 | ✅ 全量 escape | `test_html_escape`、`test_overdue_escape_and_action_buttons` |
| 已付 Unit 旧收租回调 | ✅ 直接拒绝（新防护） | `test_paid_unit_has_no_collect_button` |
| 已付后 collect 列表 | ✅ 已付 Unit 隐藏 | 同上 + S2 sanity check |

## Findings fixed during this review

- F1 未使用 import 清理（`callback.py`/`commands.py` 的 Decimal/encode/show_menu/
  build_* / pending_list_keyboard / timedelta）。
- F2 `api_client.py` get_tasks 前多余空行（风格）。
- F3 首页按钮图标与 brief 对齐：`📊 财务`（原 `💰 财务`）。
- F4 pending 键盘在无逾期时不再显示「查看全部逾期」空按钮。
- F5 确认失败重试按钮保留同一 nonce（新增 `test_confirm_error_retry_same_nonce`）。
- F6 `_begin_rent_entry` 增加「已付当月」防护，杜绝旧卡片重复登记
  （新增断言在 `test_paid_unit_has_no_collect_button`）。
- F7 空状态文案统一（`🏘 还没有房源数据`、`🎉 暂无逾期租金`、`✅ 今天没有紧急事项`）。

## Remaining UX issues (accepted for this phase)

- R1 输入金额/日期时，用户的新消息气泡仍会留在对话里（edit-first 只保证
  机器人侧单卡片变化；这是 Telegram 平台限制，未来可加 reply 引导或
  「发送后将确认页置顶」提示）。
- R2 dashboard 需要 5 个 API 并行；任务端点对只读 agent key 会 403，届时
  任务/租约区自动隐藏（已验证 S1），不会造假但信息会少。
- R3 已确认的收入在「查看付款」里展示最后一条；多次付款（如逾期多月分笔付）
  的历史列表留待后续。
- R4 收租金额默认=当月应收；逾期多月时未自动叠加总欠款（避免误写，保持
  单笔=当期应收的确定性；逾期总额在逾期页/待处理页可见）。
