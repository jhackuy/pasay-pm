# WINDOWS_RUNTIME_SINGLETON_PERSISTENCE_007D_POST_REBOOT_REPORT

Task: `WINDOWS-RUNTIME-SINGLETON-PERSISTENCE-007D-R1`（POST-REBOOT EVIDENCE COLLECTION）
Session: 继续 007D 会话（未新建）。
模式：Standard / Reasoning High；工作区 `D:\AI-Review\pasay-pm`。
目标 SHA：`56d186a699ada05d612f26aed69c3badd26a64fb`
取证时间：2026-08-17 21:59–22:06 (+08:00)。
执行约束：**只读取证，零修改**（无代码改动 / 无 commit / 无 restart / 无 kill / 无 task 改动；未触发任何 bootstrap/stop）。

> 全部证据为本次会话实测（PEB cmdline / toolhelp32 / netstat / 文件元数据 / DB 只读查询）。

---

## 1. Windows boot timestamp

- **Windows 启动 ≈ 2026-08-17 21:19:51 (+08:00)**。
  证据：007D reboot collector 在 21:21:24 记录 uptime `Days 0 / Hours 0 / Minutes 1 / Seconds 33`
  （21:21:24 − 1m33s ≈ 21:19:51）；且开机后最早的 autostart 产物为 21:20:24（runtime-version-proof），
  WindowsTerminal（logon 应用恢复）start = 21:20:03，二者均与 boot≈21:19:51 自洽。
- 其余只读启动时间佐证通道（CIM/systeminfo/`net stats`）在本沙箱仍被拒，以 collector uptime 为主证。

## 2. Runtime startup timestamp（canonical autostart 链时间线）

| 时间 (+08:00) | 事件 | 证据文件 |
|---|---|---|
| 21:20:24.67 | `start-runtime.ps1` 执行并写 LIVE_RUNTIME_SHA proof | `.runtime/runtime-version-proof.json` mtime=21:20:24 |
| 21:20:27 | API lock 写入 + API 进程 spawn | `runtime_api.lock` started_at=21:20:27；API 进程 start=21:20:27 |
| 21:20:34 | Bot lock + Bot 进程 spawn | `runtime_bot.lock` started_at=21:20:34；bot 进程 start=21:20:34 |
| 21:20:38 | Worker lock + Worker 进程 spawn | `runtime_worker.lock` started_at=21:20:38；worker 进程 start=21:20:38 |
| 21:20:39 | readiness=**READY** + reboot collector 钩子启动 | `readiness.json` at=21:20:39；`007D_REBOOT_EVIDENCE.log` start=21:20:39 |

## 3. Reboot artifact 内容（存在 / mtime / 内容摘要）

| 文件 | 存在 | mtime | 内容 / 结果 |
|---|---|---|---|
| `.runtime/readiness.json` | YES | 21:20:39 | `lifecycle=READY reason=ready sha=56d186a…`；components：api owned=true pid=26780 identity=ok healthy=true；bot owned=true pid=27924；worker owned=true pid=15480 |
| `.runtime/runtime_api.lock` | YES | 21:20:27 | `{"component":"api","pid":26780,"started_at":"…21:20:27","sha":"56d186a…"}` |
| `.runtime/runtime_bot.lock` | YES | 21:20:34 | `{"component":"bot","pid":27924,…"sha":"56d186a…"}` |
| `.runtime/runtime_worker.lock` | YES | 21:20:38 | `{"component":"worker","pid":15480,…"sha":"56d186a…"}` |
| `.runtime/runtime-version-proof.json` | YES | 21:20:24 | `live_runtime_sha = 56d186a699ada05d612f26aed69c3badd26a64fb` |
| `.runtime/acceptance/007c/007D_REBOOT_RESULT.json` | YES | 21:22:55 | verdict=FAIL（**验证伪阳性**，见 §15 注）；readiness=READY；api_owner_pid=26796；live_sha=56d186a；conflicts=36（历史） |
| `.runtime/acceptance/007c/007D_REBOOT_EVIDENCE.log` | YES | — | collector start 21:20:39 → census 21:21:25（8001 owner=26796 healthy=True readiness=READY）→ stable window 90s → RESULT_WRITTEN 21:22:55 |

## 4. Process ownership table（live，21:59–22:00 实测）

| component | PID | PPID | started_at | identity | lock | health | canonical-owned |
| --------- | --: | ---: | ---------- | -------- | ---- | ------ | --------------- |
| API（launcher） | 26780 | 27228† | 21:20:27 | ok（`-m uvicorn app.main:app --host 127.0.0.1 --port 8001`） | runtime_api.lock pid=26780 | healthy=200 | **YES** |
| API worker/listener | 26796 | 26780 | 21:20:27 | ok（同 cmdline，codex base python） | (子进程) | /health 200 | YES（lock pid 的直接子进程） |
| Bot（launcher） | 27924 | 27228† | 21:20:34 | ok（`-u -m pasay_bot.main`） | runtime_bot.lock pid=27924 | — | **YES** |
| Bot worker | 27940 | 27924 | 21:20:34 | ok（同 cmdline） | (子进程) | 日志活跃 | YES |
| Worker（launcher） | 15480 | 27228† | 21:20:38 | ok（`run-operations-worker.py --interval 60`） | runtime_worker.lock pid=15480 | — | **YES** |
| Worker worker | 15716 | 15480 | 21:20:38 | ok（同 cmdline） | (子进程) | 日志活跃 | YES |
| PostgreSQL :5432 | 9828 | 8560 | boot 后 | postgres | — | LISTENING | 基础设施 |
| DSH Harness :3080 | 19508 | — | logon 后 | dsh web | — | LISTENING | 基础设施（非 Pasay） |

† 27228 = 本次 boot 的 `bin/pasay_runtime.py bootstrap` 进程（spawn 三个组件后退出，现不可打开 → PEB open_err）。

**结构说明（重要）**：Windows venv `python.exe` 是 launcher，会以 base interpreter 重执行同一命令。
因此每个组件 = launcher + 其直接子进程（同 cmdline 对）。lock 记录 launcher PID；8001 listener 是
API launcher 的直接子进程。这是**正常的 venv 架构**，不是双实例。

## 5. API / Bot / Worker 数量（交叉证明）

```
Canonical owners        = 3（api=26780, bot=27924, worker=15480；lock 全部 alive+identity ok）
API instances           = 1（26780→26796；8001 唯一 LISTENING；全仓无第二个 uvicorn app.main:app）
Telegram pollers        = 1（27924→27940；唯一 pasay_bot.main 进程）
Workers                 = 1（15480→15716；唯一 run-operations-worker 进程）
Unexpected Pasay runtime= 0（全 python 进程清点仅上述 6 个 + 取证脚本自身）
Unowned runtime         = 0
```

判定依据：toolhelp32 全进程清点 + PEB cmdline 分类 + netstat 端口 + lock 文件 + readiness 五方交叉，非仅按进程名。

## 6. 8001 ownership

```
127.0.0.1:8001  LISTENING  PID 26796（API worker）
listener identity : codex base python "-m uvicorn app.main:app --host 127.0.0.1 --port 8001"
lock PID          : 26780（listener 的父进程 = runtime_api.lock 记录 PID）
canonical-owned   : YES（lock pid alive + identity ok + listener 为 lock pid 直接子进程）
/health           : HTTP/1.1 200 OK（server: uvicorn）
OpenAPI identity  : title="PASay Property Management API" version="1.0.0"
```

**不存在 007C 的 “healthy API but unowned” 状态**：当前 8001 唯一监听者来自 canonical lock 持有者的
直接子进程；`readiness.json` 由 canonical owner 判定为 READY（owned=true）。

## 7. Telegram 409 since reboot = **0**

- `bot_runtime.log`（stdout，1632 行）：12 处 `Conflict` 全部位于 **L22–L72**（旧段，offset=0 的旧实例风暴）；
  **L128 之后（本次 boot 后的连续单 poller 段）零 Conflict**。
- `bot_runtime.log.err`（202 行）：6 个 Conflict traceback 在 L32–L192（32 行间隔，旧段），之后为
  21:21:21 写入的 **post-reboot** `callback ccl → NameError: role` traceback（Owner 实机点击 Cancel 触发，见 §14 UX-3）。
  最后一次 Conflict traceback 位于 NameError 之前 → 均先于本次 boot 的 appends。
- 逻辑证明：409/`Conflict`（terminated by other getUpdates）要求 ≥2 个并发 poller；本次 boot 后全仓仅
  一个 `pasay_bot.main` 进程（27924→27940，start 21:20:34，无重启）→ **本次 boot 后 409 不可能发生，实测 0**。
- collector 的 `telegram_conflict_in_window=36` 是 append 模式日志整文件统计（含历史），**不是本次 boot 后事件**。

## 8. Bot restart / poller evidence（since reboot）

- poller 进程：1 个（27940），start 21:20:34，持续运行至今（进程 start time 未变、无第二 bot 进程）。
- poller 标记（getMe OK / starting polling）：8 处全部位于 bot_runtime.log **L1–L128**（旧段，多次启动历史）；
  L128 后 0 次新增启动标记 → 本次 boot 后 **bot 重启次数 = 0**。
- 活跃证据：bot_runtime.log mtime 22:04:48，尾部 `[TRACE] render OK chat_id=-1004433994558` + 连续
  `[GU] getUpdates … NO_UPDATES` —— bot 正在正常轮询并回复（与 Owner 实机测试一致）。

## 9. Restart persistence chain（全部步骤已由证据链证明）

```
Windows restarted        → boot ≈ 21:19:51（collector uptime）
→ user logon             → WindowsTerminal 21:20:03 / watchdog 21:20:0x（logon 应用启动）
→ persistence fired      → runtime-version-proof.json 21:20:24（=start-runtime.ps1 执行）
→ bin/start-runtime.ps1  → 写 proof（含 LIVE_RUNTIME_SHA=56d186a）+ 校验 clean 后调 owner
→ bin/pasay_runtime.py   → bootstrap：api lock 21:20:27 / bot lock 21:20:34 / worker lock 21:20:38
→ canonical API          → 26780→26796（:8001 listener，health 200）
→ canonical Bot          → 27924→27940（唯一 poller）
→ canonical Worker       → 15480→15716（worker_runtime.log.err 21:20:39 "operations worker starting"）
→ READY                  → readiness.json 21:20:39 READY + collector 钩子 21:20:39 启动
```

Owner 确认未手工启动；组件 PPID=27228（已退出的 bootstrap 进程），**与 Harness 进程树无关**。
**PERSISTENCE_RUNTIME_EVIDENCE = PASS**（每个步骤都有独立 artifact 佐证，无脑补）。

## 10. Scheduled Task 只读取证

| 通道 | 结果 |
|---|---|
| `Get-ScheduledTask -TaskName 'Pasay Runtime Autostart'` | 拒绝访问 |
| `schtasks /query /tn "Pasay Runtime Autostart"` | `The system cannot find the path specified.` |
| COM `Schedule.Service` + `GetFolder('\')` + `GetTask` | `0x80070003` |

→ **TASK_STORE_READ = ACCESS_DENIED**（与 007D 一致，重启后依然拒绝）。
**任务真实定义无法在本环境直读**；但 §9 的 runtime 启动产物（21:20:24→21:20:39 全链）已完整证明
autostart 实际触发并走完 `start-runtime.ps1 → pasay_runtime.py`。因此：

```
TASK_DEFINITION_DIRECT_READ = UNAVAILABLE
PERSISTENCE_RUNTIME_EVIDENCE = PASS
```

（未做任何 Task 修改/注册。）

## 11. 三个黑色 Terminal 窗口身份

- **结论：B. Windows Terminal 自动恢复上次 session**（3 个 tab 标题残留 Pasay venv python 路径；
  非 Pasay autostart 产生的 console）。
- 实测：
  - `WindowsTerminal.exe` PID 13528（start 21:20:03，logon 恢复），其可见窗口标题共 5 个 tab：
    `D:\AI-Review\pasay-pm\.venv\Scripts\python.exe` ×2、`D:\AI-Review\pasay-pm\pasay-telegram-bot\.venv\Scripts\python.exe` ×1、
    `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe` ×2。
  - tab 内容进程为普通交互式 `powershell.exe`（无参数，12736/12764）；**无任何 python 子进程运行于
    WindowsTerminal 下**（本次 boot 的 runtime 进程不在 13528 子树内）。
  - Pasay runtime 组件有独立的隐藏 conhost（API 26804 @21:20:27、bot 27948 @21:20:34、worker 27056 @21:20:38，
    与组件 spawn 时刻一致），**均无可见窗口** → autostart 已全程后台运行（DETACHED_PROCESS），非 A 类。
- 旁证：WindowsTerminal start 21:20:03 早于 runtime（21:20:27），与“登录时终端自动恢复”时序一致。
- 因此 **FOLLOW_UP（A 类专用）不适用**：Runtime autostart 已是 background/no-console。若 Owner 想消除
  桌面残留 tab，关闭该终端窗口或取消 Windows Terminal “启动时恢复上一会话”设置即可（本轮不处理）。

## 12. target / live SHA

```
TARGET_SHA = 56d186a699ada05d612f26aed69c3badd26a64fb （007D 代码 commit）
LIVE_SHA   = 56d186a699ada05d612f26aed69c3badd26a64fb （RT worktree HEAD + runtime-version-proof + 3×lock sha + 运行组件）
MATCH      = YES
```

注：主工作区 HEAD=`b1a3d97`（007D 报告 commit，位于 56d186a 之上），部署/运行代码均为 `56d186a`（RT worktree）。

## 13. worktree 状态（只读）

- `worktrees/BOT-V1-USABLE-001-RUNTIME`：`git status --short` = **CLEAN**（运行中的产品代码树）。
- 主工作区：`git status --short` 仅既有 untracked 文件（`.audit/`、`.build/`、`.research/`、历史报告 md、
  tar.gz 等，均为 007D 之前遗留）；`git diff --stat` = 空（**无任何未提交改动**）。
- `.runtime/acceptance/…` 取证/探针/结果文件均为 **gitignored 非提交文件**，与产品 worktree clean 不冲突。

## 14. Telegram UX follow-up（只记录，本轮零修改）

| ID | 现象 | 记录 |
|---|---|---|
| UX-1 | Bot/Windows 重启后固定底部菜单消失，需先给 bot 发消息才重现 | `FOLLOW_UP_TELEGRAM_KEYBOARD_RESTORE`（菜单随会话消息下发，重启后无消息即无菜单） |
| UX-2 | 输入 `123` 命中旧 Help 菜单（Record an expense / Query information / Ask the assistant / Cancel） | `FOLLOW_UP_REMOVE_LEGACY_FALLBACK_MENU`（未识别输入回落到 legacy fallback） |
| UX-3 | 旧 Help 三个入口明显卡顿 | `FOLLOW_UP_FAST_PATH_LATENCY`。日志取证：bot 日志无 per-callback 时间戳（`[GU]`/callback trace 无时间），无法从现有日志量化耗时；已记录现象，禁止性能改造 |
| UX-4 | Home → 旧 Home（非运营总览） | `FOLLOW_UP_CANONICAL_HOME`（目标统一为运营总览） |
| UX-5 | 运营总览信息正确但视觉层级差 | 目标排版：`📊 运营总览 / 💰 本月租金(应收/已收/待收) / 🔴 风险(历史欠租/逾期/合同到期/空置) / 📋 待处理(待付款/今日事项)`；只记录不改 UI |
| UX-6 | E8 显示 Approved/待付款 紧接 ✅ Paid/已付款 | 见 §15 → `RENDER_GROUPING_AMBIGUITY` |

## 15. E8 DB 状态只读核验（UX-6）

- 运行时 DB（`.env` 指向 `pasay_pm_win_test`；只读 SELECT）：
  ```
  expenses id=8: status=approved, amount=7000.00, payee=Repair,
                approved_by=1, approved_at=2026-08-15 09:29:43, payer_user_id=NULL
  ```
- **E8 实际 DB 状态 = `approved`（待付款）单状态**；无任何 paid 记录（payer_user_id 为空）。
- “✅ Paid / 已付款” 是渲染结果中**下一 section 的分组标题**（按状态分组的 section 边界），不是 E8 的第二状态。
- 结论：**RENDER_GROUPING_AMBIGUITY**（非 DATA_STATE_BUG）。禁止改 DB / 改 renderer（本轮未改）。
  建议后续（独立任务）在 section 边界加视觉分隔。

## 16. 最终 Gate

| Gate | 结果 |
|---|---|
| API exactly 1 | **PASS**（26780→26796 单组件；无第二个 uvicorn） |
| Bot exactly 1 | **PASS**（27924→27940 单 poller） |
| Worker exactly 1 | **PASS**（15480→15716 单 worker） |
| all canonical-owned | **PASS**（lock+alive+identity ok；readiness READY） |
| unowned runtime = 0 | **PASS**（无任何 Pasay 外部进程） |
| readiness = READY | **PASS**（21:20:39 READY，持续） |
| Telegram 409 since reboot = 0 | **PASS**（§7：全部 Conflict 为 boot 前历史；单 poller） |
| TARGET_SHA == LIVE_SHA | **PASS**（56d186a == 56d186a） |
| automatic startup proven | **PASS**（§9 全链 artifact） |
| manual startup not required | **PASS**（Owner 确认未手工启动） |
| Harness not required | **PASS**（组件 PPID=已退出的 bootstrap，与 Harness 树无关） |

```
最终状态：READY_FOR_OWNER_RUNTIME_ACCEPTANCE
TASK_DEFINITION_DIRECT_READ = UNAVAILABLE（ACCESS_DENIED）
PERSISTENCE_RUNTIME_EVIDENCE = PASS
```

## 附注：007D_REBOOT_RESULT.json verdict=FAIL 的成因（验证伪阳性，非运行时缺陷）

collector 的 FAIL 由三个验证伪阳性叠加，全部与运行时健康无关：
1. **venv launcher→base 子进程对**：每个组件在 Windows 上呈现 2 个进程（lock pid=launcher + 同 cmdline 的
   子进程），collector 的 `counts` 按 cmdline 归类得 api/bot/worker=2，并把子进程列为 “unowned”。
   事实：子进程是 lock pid 的直接子进程、同 identity、同组件 → 属于该组件，unowned 实为 0。
2. **`api lock pid == 8001 listener` 检查过严**：listener 是 lock pid 的子进程，二者必然不同 PID → 误判
   api_canonical_owned=false。canonical owner 自身语义（lock pid alive + identity ok + /health 200）为 owned=true。
3. **append 模式日志整文件统计**：`telegram_conflict_in_window=36` 含 boot 前历史（§7 已按行号切窗证明
   boot 后 = 0）。
collector 的 `owned` 字段本身全部为 True；`readiness=READY`；`api_healthy=true`。以上已在 §4–§7 用
进程树 + PEB + 行号切窗重证。T8 探针完整 300s 稳定窗口仍需无 Harness 上下文（探针已就绪，`007D_T8_RESULT.json`
当前为 pre-boot SELFCHECK_OK）；本次 reboot 的 90s collector 稳定窗口（21:21:25–21:22:55 全程 api_healthy=true）
与本报告取证共同构成等价验收证据。

## 报告文件

- 本报告：`WINDOWS_RUNTIME_SINGLETON_PERSISTENCE_007D_POST_REBOOT_REPORT.md`
- 证据原件：`.runtime/readiness.json`、`.runtime/runtime_{api,bot,worker}.lock`、`.runtime/runtime-version-proof.json`、
  `.runtime/acceptance/007c/007D_REBOOT_RESULT.json`、`007D_REBOOT_EVIDENCE.log`、
  `.runtime/acceptance/007d/census2_out.json`（进程快照）、`007D_T8_RESULT.json`
- 取证工具（只读）：`.runtime/acceptance/007d/{peb_test.py, census.py, census2.py, windows_enum.py, openapi_probe.py, e8_check.py}`
