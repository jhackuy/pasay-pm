# WINDOWS_RUNTIME_SINGLETON_PERSISTENCE_007C_REPORT

Task: WINDOWS-RUNTIME-SINGLETON-PERSISTENCE-007C
Session: NEW Harness session (007B not archived).
Target SHA: `b4c25dcc79ceda1e346a5cff1b0a152b39d59580`
Mode / Reasoning: Standard / High.
Date: 2026-08-17 (live census at 20:19 +08:00).

---

## 0. 执行方式

本任务以「重新取证 + 生产验收」为主，**未修改任何 007B Runtime / 产品代码**。
所有取证脚本与验收探针都放在**非提交**临时目录：

```
.runtime/acceptance/007c/
```

工作区 `git status` 仅含既有的 untracked 报告文件；**无产品文件被改动**（HEAD 保持
`b4c25dcc`，runtime worktree clean）。探针文件见 §9。

---

## 1. PID 39464 最终身份（最终分类 = B）

**结论：PID 39464 = 一个 Pasay 生产 runtime 进程（生产 API），由"第二个启动入口"拉起的
孤儿进程；不是 Harness 基础设施进程。** 按任务 §二 的三种分类，判定为 **B. Pasay child process
（第二启动入口产物）**。

### 1.1 关键取证（均为本会话实测）

| 维度 | 证据 |
|---|---|
| EXE path | `C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe` |
| Start time | `2026-08-17 19:52:42` |
| PPID | `45868` = `D:\AI-Review\pasay-pm\.venv\Scripts\python.exe`（repo venv，仍存活） |
| TCP listening | `127.0.0.1:8001` LISTENING（唯一 8001 listener） |
| Remote conns | `[::1]:6808→[::1]:5432` ESTABLISHED（连接 **Pasay PostgreSQL**，DB owner=9404） |
| 内部回环 | `127.0.0.1:6804<->6805` ESTABLISHED（API 进程内部回环对） |
| HTTP | `/health`=200；`/openapi.json` title=`PASay Property Management API version=1.0.0`（**确为 Pasay 生产 API**） |
| 归属 | `bin/pasay_runtime.py status` → `api: lock=False owner=0 alive=False healthy=True`；`readiness.json=STOPPED` |

### 1.2 "codex-runtimes python" 不是 Harness 本体

`D:\AI-Review\pasay-pm\.venv\pyvenv.cfg` 明文：

```
home = C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python
executable = ...\python.exe
command = ...\python.exe -m venv D:\AI-Review\pasay-pm\.venv
```

即 **repo `.venv` 的基底解释器就是这个 codex-runtimes python**。因此 PID 39464 的 exe 路径是
"codex-runtimes python" 只是说明它是 Pasay venv 的基底 Python，**并非** "Harness 自身进程实据"。
007B 报告中"39464 = harness 自身进程占用 8001"的定性，被本报告否决。

### 1.3 进程树证据（PID/PPID/ancestry）

由 toolhelp32 快照（CIM 在该节点被拒，改用 ctypes 枚举进程树）：

```
~~ 37160 (Harness watchdog, 本会话内已重启/更替, PID 非固定)
   -> 29072 cmd.exe
      -> 17284 node.exe   (Harness 服务根, 启动于 8/16 19:22)
         -> 38404 cmd.exe
            -> 7548 node.exe   (Harness 3080 listener, state.json listen_pid=7548)
               -> 27900 node.exe  (任务宿主, 启动于 19:52:41)
                  -> 40688 powershell.exe  (19:52:42)
                     -> 45868 python.exe   (repo venv, 19:52:42)
                        -> 39464 python.exe (Pasay API on :8001, 19:52:42)
```

结论：PID 39464 整体位于 **Harness 派生（node→powershell→python）的进程子树内**，
且 45868（repo venv python）仍存活、并作为 39464 的直接父进程 —— 这是 **"由 Harness 执行的
某一个命令/脚本启动的一个 Pasay runtime"** 的形状，而不是规范 owner（`pasay_runtime.py` 会短暂
spawn 后退出，且不会让 API 进程以 `codex base python` 直连路径残留于此）。

### 1.4 导致"第二个启动入口"的机制

- **残留直启脚本仍存在（绕过 owner）**：
  - `D:\AI-Review\pasay-pm\.ai-control\tmp\start_runtime.ps1`：直接
    `Start-Process python -m uvicorn app.main:app --host 127.0.0.1 --port 8001`
  - `D:\AI-Review\pasay-pm\.ai-control\tmp\start_backend.ps1`：同样直接 `uvicorn app.main:app`
  - 这两条是**绕过 `bin/pasay_runtime.py` 的第二入口**。
- 一个 Harness 宿主 shell 在 19:52:41–42 执行了某条命令，产生 `45868→39464` 的
  API 监督/子进程对；该 API **全程未被规范 owner 拥有**（无 `runtime_api.lock`，owner=0）。

---

## 2. PID/PPID/process tree 证据

见 §1.3。附加：所有"仍未退出并仍由 Harness 派生挂起 8001"的关键进程：

| PID | exe | 角色 | 父 |
|---|---|---|---|
| 39464 | codex base python | **Pasay API :8001** | 45868 |
| 45868 | `.venv\Scripts\python.exe` | API 直接父（repo venv python，存活） | 40688 |
| 40688 | powershell | Harness 执行过的一条命令宿主 | 27900 |
| 27900 | node | Harness 任务宿主（19:52:41） | 7548 |
| 7548 | node | **Harness 3080 listener** | 38404 |
| 17284 | node | Harness 服务根 | 29072 |
| 9404 | (postgres) | **Pasay PostgreSQL 5432** | - |

不存在需要额外解释的其他异常 python（当前无 bot / worker / 其他 Pasay python）。

---

## 3. 8001 ownership

当前（20:19 +08:00）：

```
TCP 127.0.0.1:8001  0.0.0.0:0  LISTENING  39464
```

| 计数 | 值 | 判定 |
|---|---|---|
| API listener count | **1** | 唯一 |
| canonical owner PID | None（owner 不持有此 listener，`runtime_api.lock`=False） | **未由 owner 拥有** |
| unexpected owner PID | **39464** | **第二入口孤儿 API** |

> 说明：规范 owner `bin/pasay_runtime.py status` 显示 `api: healthy=True`（HTTP 探活成功）但
> `owner=0 alive=False` —— 即 8001 上有一个健康但不被 owner 管理的 API。

---

## 4. Telegram Poller Ownership（不泄露 Token）

- **canonical bot PID**：当前无存活 bot。历史上被 owner 管理过的 bot（如 007B 报告的 25400）
  在 409 风暴后已退出。
- **unexpected competing poller PID**：`bot_runtime.log` 显示 **3 次 `getMe OK: @pasayhousebot` /
  3 次 `starting polling` / 12 次 `Conflict`** —— 即**最多出现了 3 个并发 bot poller**。
- **PID 39464 不是 poller**：39464 是 FastAPI/uvicorn 应用（无 Telegram polling），
  407B"39464 抢 bot token"的归因被否决。
- 时间线：
  - 19:36:08 worker#1；ark 19:52:35 api/worker 又启动；19:52:41–42 node→…→39464(API)；
    19:53:52 worker 再启动；19:54:30–31 bot 退出 / readiness=STOPPED。
  - 多个 bot 实例由"第二个启动入口 + 反复 bootstrap"产生，触发 Telegram 409。

**结论**：409 的根因是**多个（>1）bot poller**（由第二启动入口/重复启动造成），**不是** PID 39464
（API）。当前 409 已停（无 bot 存活窗口内 conflict=0），但不代表 owner 已收敛为单一 owner。

---

## 5. 所有生产启动入口（枚举，未修改）

**规范（唯一 intended owner）**
- `bin/start-runtime.ps1`（+ worktree 副本）→ 委托 `bin/pasay_runtime.py bootstrap`。
- `bin/install-runtime-task.ps1`（+ worktree 副本）→ 注册 "Pasay Runtime Autostart" → start-runtime.ps1。

**发现的其他生产启动入口（均为潜在第二 owner / 直启 API 绕过 owner）—— 先报告，不擅自删除：**
- `D:\AI-Review\pasay-pm\.ai-control\tmp\start_runtime.ps1` —— 直接 `uvicorn app.main:app --port 8001`（绕过 owner）
- `D:\AI-Review\pasay-pm\.ai-control\tmp\start_backend.ps1` —— 直接 `uvicorn app.main:app --host 127.0.0.1 --port 8001`（绕过 owner）
- 其他 `.runtime\*.ps1` 启动脚本（`start_runtime.ps1`/`start_runtime_v1.ps1`/`restart_api.ps1`/
  `start_bot_v1.ps1`）均已改为**委托规范 owner**（fail-closed），不再直启。

**结论：生产仍存在 2 个绕过 owner 的直启入口（legacy `.ai-control/tmp/`）→ 入口审计 FAIL。**

> 补充：本沙箱内 `Get-ScheduledTask` / `schtasks` / 注册表 TaskCache 均 **拒绝访问**，
> 无法确认 007B 声称已注册的 "Pasay Runtime Autostart" 任务真实存在与否（见 §11 阻断）。

---

## 6. Harness 是否真的构成竞争？

- **Harness 自身**（node/3080）不是 Pasay 进程，不抢占 8001、不跑 bot token。
- 但 **Harness 派生出的 shell 子树**执行了"第二个 Pasay 启动入口"，把 Pasay API 拉起来后
  一直挂在这个 Harness 子树下 —— 这是 **Harness 宿主环境作为启动源**，构成事实上的竞争源，
  而非直接基础设施抢占。
- 因此"是否 Harness 在线就必然竞争"需在**无 Harness** 的生产验收（T8 §7）中证明，
  本沙箱（Harness 在线 3080/7548）**无法干净证明**。

---

## 7. 无 Harness 环境 T8 验收方案（已交付 + 已逻辑验证）

在 `.runtime/acceptance/007c/t8_probe.py` 交付了一个**独立于 Harness 进程运行的探针**：

- Phase1 `wait_harness_gone`：等待 Harness 3080 listener（及 dev task 宿主）消失后才继续；
  若 Harness 仍在 → **fail-closed** 退出 `NOT_EXECUTED_HARNESS_PRESENT`（本次实测即为该结果，
  因本会话在 Harness 内）。
- Phase2 读取：8001 owner 及其 ancestry、lock 文件、readiness、live SHA。
- Phase3（可选 `--bootstrap`）：恰一次调用规范 `bin\start-runtime.ps1`，等待 READY。
- Phase4 稳定窗口：采样 Telegram conflict（`Conflict` 行数）、poller 标记、API health。
- 结果原子写入 `007C_T8_RESULT.json` + `007C_T8_EVIDENCE.log`。

探针 `--selfcheck` 已跑通（进程清点 353-354 / 8001 解析正确 / 原子写成功），路径修复后
`sha` 正确解析为 `b4c25dcc…`。**本次未执行真实 bootstrap**（Harness 在线会污染 T8）。

> Run：退出 Harness 后，在交互式 PowerShell 运行：
> `.venv\Scripts\python.exe .runtime\acceptance\007c\t8_probe.py --bootstrap --wait-harness 7200 --window 300`

---

## 8. 409 稳定窗口结果

**本沙箱内无法获得有意义的 409=0 稳定窗口**，因为：
1. 当前无 bot 存活 → 窗口内 conflict=0 无代表性；
2. Harness 在线，T8 bootstrap 会再次制造第二启动入口/竞争条件；
3. 规范 owner 当前并不拥有任何组件（readiness=STOPPED 但 8001 有孤儿 API）。

因此把 409 稳定窗口验证**留给无 Harness T8（§7）与真实重启（§10）**。
历史 `bot_runtime.log` 证明：owner 之外的第二入口可同时拉起 3 个 bot → 12 次 Conflict。

---

## 9. reboot collector 结果（本次不重启，仅交付并验证）

- 交付 `reboot_collector.py`：真实重启+登录后自动记录 boot uptime、owner 组件 PID、
  API/Bot/Worker PID、8001 ownership、readiness、live SHA、startup/`.err` 日志、Telegram
  conflict 与 restart-loop 判定；原子写 `007C_REBOOT_RESULT.json` + `007C_REBOOT_EVIDENCE.log`。
- 交付 `register-reboot-collector.ps1`：注册一次性 At-Logon 任务 "Pasay Boot Evidence Collector 007C"。
- 验证：本次非破坏性运行（delay=0）正确产出 `verdict=PARTIAL, api_owner=39464, sha=b4c25dcc…,
  readiness=STOPPED`，与 live 状态一致，证明收集逻辑可用。
- **注册被沙箱拒绝**：`New-ScheduledTaskAction : 拒绝访问 (0x80041003)`。→ 注册/验证需 Owner
  在交互式/提升会话执行（见 §12）。

**本任务按要求未执行、也不会执行 Windows reboot。**

---

## 10. 重启后的最终验收条件（目标，未达）

真实重启后需证明：
```
Scheduled Task fired / canonical owner alive / API=1 / Bot=1 / Worker=1 /
health=PASS / readiness=READY / Telegram 409=0 / TARGET_SHA==LIVE_SHA / Harness not required /
manual PowerShell not required
```
**尚未达到**（boot collector 未实跑，T8 未在无 Harness 下执行，autostart task 无法在该沙箱确认）。

---

## 11. API / Bot / Worker 数量（当前 live）

| 组件 | 数量 | 归属 |
|---|---|---|
| API on :8001 | **1** | **孤儿（owner=0，第二入口产物 PID 39464）** |
| Bot poller | **0**（当前无存活） | - |
| Worker | **0**（当前无存活） | - |
| Harness 3080 | 1 | 7548（合法 Harness 基础设施） |
| PostgreSQL 5432 | 1 | 9404 |

规范 owner：unit owned=False；api/bot/worker 均 `lock=False owner=0`。**owner 当前不拥有任何组件。**

---

## 12. target / live SHA

```
TARGET_SHA = b4c25dcc79ceda1e346a5cff1b0a152b39d59580 (007B commit)
LIVE_SHA   = b4c25dcc79ceda1e346a5cff1b0a152b39d59580 (RT worktree clean)
TARGET == LIVE : YES
```

---

## 13. 是否修改代码

- **未改任何产品 / Runtime 代码**。`git status` 无产品改动；worktree clean。
- 仅新增非提交取证/验收文件：
  - `.runtime/acceptance/007c/t8_probe.py`、`reboot_collector.py`、`register-reboot-collector.ps1`
  - `.runtime/acceptance/007c/probe_processtree.py`、`probe_cmdline_peb.py`、`probe_modules.py`
  - 快照/结果：`007C_T8_RESULT.json`、`007C_T8_EVIDENCE.log`、`007C_REBOOT_RESULT.json`、
    `007C_REBOOT_EVIDENCE.log`、`proc_snapshot.json`

---

## 14. 最终状态

**`007C_CODE_DEFECT_FOUND`**（发现真实代码/启动治理缺陷，需先修再审）。

### 证据摘要
1. **PID 39464 = 第二启动入口拉起的孤儿 Pasay API**（B 类），运行于 Pasay venv 基底 Python，
   绑定 Pasay Postgres；owner 不拥有它（lock=False owner=0），readiness=STOPPED 而 API 仍存活服务。
2. **存在绕过 owner 的直启入口**：`.ai-control/tmp/start_runtime.ps1`、`start_backend.ps1`
   直接 `uvicorn app.main:app` on 8001（生产启动入口审计 ≠ 唯一入口）。
3. **owner 的 API 归属存在缺陷**：`pasay_runtime.py _bootstrap` 在 8001 已有任一健康 API 时
   `[skip] api: already healthy` 且**不写 `runtime_api.lock`、不校验/接管真实监听 PID**，随后仍写
   `lifecycle=READY`。于是出现"READY 却无 API 所有权、健康却不被管理"的孤儿状态（= §3/§11 live 态）。

### 最小修复建议（不堆 workaround，不改产品）
在 `bin/pasay_runtime.py` 的 `_bootstrap()` 中，`name=="api" and api_healthy()` 分支改为：

```
if api_healthy():
    # 找到 :8001 真实监听 PID（netstat/owner），尝试原子接管 runtime_api.lock：
    live_pid = _port_owner(8001)      # 需新增：解析 netstat -ano 的 8001 owner
    if live_pid and _claim_component("api", live_pid, sha=sha):
        started["api"] = live_pid      # 接管并记录真实 PID -> 成为 owner
    else:
        # 无法接管（已被别处拥有）-> 不写 READY，写 FAILED(reason="unowned-api")
        ok = False
        continue
```

这样：(a) 唯一 API 必须被 owner 拥有才 READY；(b) 不会误杀无关进程（仍用 O_EXCL 锁 + 存活校验）；
(c) 消除"第二入口孤儿 API"导致的 owner 空转/幽灵 READY。

### 仍需（非本沙箱可得）
- 清理/CONFIRE 第二启动入口（至少 `.ai-control/tmp` 直启脚本）→ 待 Owner 决定，非本任务擅自删除。
- 无 Harness T8 执行（§7）。
- Owner 注册 boot collector 并执行真实重启（§9/§10），用 `007C_REBOOT_RESULT.json` 证明重启持久性。

> 按任务 §十：非第 3 状态才算完成。当前为第 1 状态（发现真实缺陷），**007B/007C 未达最终验收**。
