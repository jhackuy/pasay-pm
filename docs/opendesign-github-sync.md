# OpenDesign → GitHub 自动同步方案

将本地 Open Design 桌面端项目增量同步到 GitHub `pasay-opendesign` 仓库的 `opendesign/live` 分支，只发布 allowlist 指定的文件，通过 gate runner 验证后再提交推送。

---

## 一、权威配置

### 1.1 源路径（Source of Truth）

```
C:\Users\Admin\AppData\Roaming\Open Design\namespaces\release-stable-win\data\projects\c5fb3a39-c6d0-4003-9cee-66deb7a626a1
```

- 该路径必须真实存在，且 `Resolve-Path` 解析后必须与上面字面字符串 **完全相等**（防止符号链接跳变）。
- 一旦不一致，同步会立即 BLOCKED，不会继续。

### 1.2 目标仓库与分支

| 项 | 值 |
|---|---|
| GitHub 仓库 | `https://github.com/jhackuy/pasay-opendesign.git` |
| Remote 名称 | `origin` |
| 发布分支 | `opendesign/live` （写入目标） |
| 基线分支 | `main`（仅首次建分支时参考，**禁止任何写入**） |

### 1.3 Allowlist（白名单）

同步时 **只会复制** 以下 8 个文件，其他所有文件一律不会从源路径进入 mirror：

```
index.html
pasay-design-system.html
pasay-mini-app.html
pasay-telegram-bot.html
gates-runner.js
deepseek.svg
minimax.svg
pasay-mini-app-preview.png
```

### 1.4 禁止列表（正则匹配任意路径片段）

如果 mirror 目录中扫描到任何文件路径匹配以下正则（大小写不敏感），直接 BLOCKED：

```
\.git\\|^\.env|secrets?|token|password|credentials|conversations|logs?\\|database|runtime|namespace|\.zip$|\.bak$|cache|screenshots
```

典型命中示例：
- `.env` / `.env.local`
- 任何路径含 `token` / `password` / `credentials`
- `secrets/` 目录、`logs/` 目录
- `*.zip`、`*.bak` 文件
- `cache/`、`screenshots/` 目录

---

## 二、架构（文本图）

```
  +----------------------------------------------------------------------------------+
  |  Open Design 本地项目 (Source of Truth)                                          |
  |    C:\...\projects\c5fb3a39-c6d0-4003-9cee-66deb7a626a1                          |
  +-------------------------------------+--------------------------------------------+
                                        |
                                        |  FileSystemWatcher
                                        |  (NotifyFilters: FileName, DirectoryName,
                                        |   LastWrite, Size, Attributes)
                                        v
  +----------------------------------------------------------------------------------+
  |  watch.ps1                                                                       |
  |    ├─ initial catch-up sync.ps1                                                  |
  |    ├─ 事件记录 lastEventAt + dirty 标记                                          |
  |    ├─ System.Timers.Timer 500ms tick                                             |
  |    └─ 当 (Now - lastEventAt >= 2500ms) AND dirty → 调用 sync.ps1                 |
  +-------------------------------------+--------------------------------------------+
                                        |
                                        v
  +----------------------------------------------------------------------------------+
  |  sync.ps1 （幂等 idempotent，可单独跑）                                          |
  |                                                                                  |
  |  ① Preflight                                                                     |
  |     - 校验源路径存在且 Resolve-Path 严格匹配                                     |
  |     - 若 mirror 不存在 → git clone pasay-opendesign 到 .ai-control/opendesign-mirror |
  |                                                                                  |
  |  ② Mirror Sync Pre                                                               |
  |     - git fetch --no-tags origin                                                 |
  |     - 若 origin/opendesign/live 存在 → checkout + merge --ff-only                |
  |       否则 → checkout -b opendesign/live origin/main (首次)                       |
  |     - git reset --hard HEAD ; git clean -fdx                                     |
  |     - 记录 BASE_SHA = git rev-parse HEAD                                         |
  |                                                                                  |
  |  ③ Allowlist Copy + 扫描                                                         |
  |     - 只复制 Allowlist 列出的 8 个文件                                           |
  |     - 每个文件前 256 字节扫描 ASCII 正则：                                       |
  |       ghp_ / github_pat_ / sk- / GITHUB_TOKEN → BLOCKED_SECRET_SUSPECT           |
  |     - 全局扫 mirror 所有文件路径 → 禁止列表正则命中即 BLOCKED_FORBIDDEN_FILE     |
  |                                                                                  |
  |  ④ Gate                                                                          |
  |     - 存在 gates-runner.js → node gates-runner.js                                |
  |       exit != 0 → BLOCKED_GATE_FAILED（不 commit 不 push）                       |
  |     - 不存在 → WARNING 默认 PASS                                                 |
  |                                                                                  |
  |  ⑤ Diff & Commit & Push（禁止 git add . / -A / --force）                         |
  |     - 对 allowlist 每项逐 git status --porcelain，收集 changed 数组              |
  |     - git diff --cached --check → 空白错误 → BLOCKED_DIRTY_WHITESPACE            |
  |     - changed 为空 → NO_CHANGE exit 0                                            |
  |     - git commit "sync(opendesign): project c5fb3a39 @ <ts>"                     |
  |       附 Allowlist 列表 + Base SHA                                               |
  |     - git push origin HEAD:refs/heads/opendesign/live （无 --force）             |
  |       失败 → BLOCKED_PUSH_FAILED                                                 |
  +-------------------------------------+--------------------------------------------+
                                        |
                                        v
  +----------------------------------------------------------------------------------+
  |  GitHub: jhackuy/pasay-opendesign @ opendesign/live                              |
  |    (只包含 allowlist 8 个文件 + 可能的 gates-runner 校验后提交)                   |
  +----------------------------------------------------------------------------------+
```

---

## 三、使用说明

所有脚本位于仓库：`scripts/opendesign-sync/`

### 3.1 手动运行一次性同步（调试 / catch-up）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/opendesign-sync\sync.ps1
```

常用参数：
- `-DryRun`：走完 gate 后直接 exit 0，不 commit 不 push。
- `-Quiet`：只写日志不输出到 stdout。

示例：
```powershell
# 先 dry-run 看 gate 是否通过
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\opendesign-sync\sync.ps1 -DryRun

# 真正同步
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\opendesign-sync\sync.ps1
```

### 3.2 启动监听器（前台常驻）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\opendesign-sync\watch.ps1
```

- 启动时先跑一次 initial catch-up sync；
- 之后挂载 FileSystemWatcher 监听 OD 源目录；
- 每次文件事件会标记 dirty，内置 2500ms debounce（500ms 轮询检查），等文件活动平息后再调用 sync.ps1；
- `Ctrl+C` 退出，退出时清理 `.ai-control/logs/opendesign-sync/watcher.pid`。

参数：
- `-DebounceMs 2500`：去抖窗口，默认 2500ms。
- `-IntervalMs 500`：定时器 tick，默认 500ms。

### 3.3 安装 Windows 计划任务（开机登录自启）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\opendesign-sync\install-task.ps1
```

Task 详情：

| 字段 | 值 |
|---|---|
| 名称 | `Pasay OpenDesign Sync Watcher` |
| 触发器 | AtLogOn（当前用户） + `PT30S` 延迟 |
| 执行程序 | `powershell.exe` |
| 参数 | `-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "<repo>\scripts\opendesign-sync\watch.ps1"` |
| 工作目录 | repo root |
| MultipleInstances | `IgnoreNew`（重复触发不启动第二份） |
| RestartCount | 3 |
| RestartInterval | 1 分钟 |
| ExecutionTimeLimit | 0（无限制） |
| StartWhenAvailable | ✅ |
| Principal | Interactive + Limited（非管理员） |

验证：
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\opendesign-sync\install-task.ps1 -Verify
```
会导出已注册 Task 的 XML 并打印关键字段。

### 3.4 卸载计划任务

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\opendesign-sync\uninstall-task.ps1
```

- 调用 install-task.ps1 `-Unregister`；
- 额外删除 `watcher.pid` 文件，如果 PID 进程仍存活会先尝试 `Stop-Process`；
- 最后输出 `UNINSTALLED`。

---

## 四、日志路径

全部位于：
```
.ai-control/logs/opendesign-sync/
```

| 文件 / 模式 | 说明 |
|---|---|
| `sync-YYYYMMDD-HHmmss.log` | 每次 `sync.ps1` 运行的独立日志 |
| `watch-YYYYMMDD-HHmmss.log` | 每次 `watch.ps1` 启动的独立日志 |
| `watcher.pid` | watcher 运行 PID（启动时校验单例；正常退出清理） |
| `task-definition.json` | `install-task.ps1` 注册后写入的快照 JSON，便于审计 |

---

## 五、回滚手册（谨慎操作）

任何异常想"彻底回到干净状态"，按下面顺序：

1. **删除计划任务**
   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\opendesign-sync\uninstall-task.ps1
   ```

2. **停掉可能残留的 watcher**
   - 查看 PID 文件内容：`.ai-control/logs/opendesign-sync/watcher.pid`
   - 任务管理器检查 powershell 进程，必要时手动结束。

3. **删除本地 mirror 目录**（下次 sync 会重新 clone）
   ```
   .ai-control/opendesign-mirror/
   ```

4. **删除远端 `opendesign/live` 分支（最谨慎的最后一步）**

   这一步是 **破坏性操作**，删除前请确认：
   - 真的需要清零所有已发布记录；
   - 没有其他人在基于该分支做开发 / PR。

   命令（在你有写权限的任何 git 客户端执行）：
   ```powershell
   git remote -v   # 确认 origin 指向 jhackuy/pasay-opendesign
   git push origin --delete opendesign/live
   # 等价于:
   git push origin :opendesign/live
   ```

   删除后，下次 `sync.ps1` 会以 `origin/main` 为基线重新创建 `opendesign/live`。

---

## 六、BLOCKED 码表

`sync.ps1` / `watch.ps1` 所有 `SYNC_BLOCKED_XXX` 状态码与含义：

| 日志标记 | exit | 触发条件 | 后续处理建议 |
|---|---|---|---|
| `SYNC_BLOCKED_SOURCE_MISSING` | 2 | OD 源路径不存在 | 检查 Open Design 是否安装并打开过指定项目；路径是否被重定位 |
| `SYNC_BLOCKED_SOURCE_MISMATCH` | 2 | `Resolve-Path` 结果≠字面路径 | 源路径被换成符号链接 / 快捷方式，恢复真实目录 |
| `SYNC_BLOCKED_MIRROR_INIT` | 2 | 首次 `git clone` 失败 或 `git fetch`/建分支失败 | 检查网络、GitHub 凭证、磁盘权限 |
| `SYNC_BLOCKED_NON_FAST_FORWARD` | 2 | `git merge --ff-only origin/opendesign/live` 失败 | 有人在远端对 `opendesign/live` 做了 rebase/force push；手动 `git log` 排查并 reset 到一致状态 |
| `SYNC_BLOCKED_FORBIDDEN_FILE` | 2 | mirror 下某文件路径命中禁止列表正则 | 删除 mirror 下对应文件，排查 allowlist 是否间接引入了子目录中的敏感文件 |
| `SYNC_BLOCKED_SECRET_SUSPECT` | 2 | allowlist 文件前 256 字节扫描到 `ghp_` / `github_pat_` / `sk-` / `GITHUB_TOKEN` | 立刻检查源文件，token 绝不应该出现在设计导出文件中 |
| `SYNC_BLOCKED_GATE_FAILED` | 3 | `node gates-runner.js` 非零退出 | 修复 gates-runner 校验报错后再同步（DryRun 模式可快速验证 gate） |
| `SYNC_BLOCKED_DIRTY_WHITESPACE` | 4 | `git diff --cached --check` 发现行尾空白 / 冲突标记等 | 修复 changed 文件的空白问题 |
| `SYNC_BLOCKED_PUSH_FAILED` | 5 | `git push origin HEAD:refs/heads/opendesign/live` 失败（无 --force） | 99% 是远端有非 fast-forward 更新，先手动 `git fetch origin opendesign/live` 再处理 |
| `SYNC_BLOCKED_WATCHER_ALIVE` | 6 | 启动 `watch.ps1` 时发现 `watcher.pid` 指向的进程仍存活 | 先确认是否真有第二份 watcher 在跑，若为误报则删除 pid 文件重启 |

正常成功状态：
- `NO_CHANGE` (exit 0)：allowlist 文件与 HEAD 完全一致，无需 commit。
- `SYNC_OK` (exit 0)：gate 通过 → commit → push 完整闭环成功。日志中包含 `Base SHA`、`New SHA`、`Changed` 列表。
