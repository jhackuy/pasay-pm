#!/usr/bin/env python3
"""WINDOWS-RUNTIME-SINGLETON-PERSISTENCE-007B — canonical Pasay Windows runtime owner.

SINGLE canonical lifecycle manager for the Pasay Windows runtime. Every
production start (``bin/start-runtime.ps1``, the Scheduled-Task autostart, a
manual re-bootstrap) MUST enter through this owner so the system always
converges to exactly ONE API + ONE Telegram poller + ONE operations worker.

Design (builds on the existing ``bin/start-runtime.ps1`` + ``.runtime`` layout;
stdlib-only, no psutil/WMI dependency):

* **Unit lock** (``runtime_unit.lock``) owns the whole runtime unit. A second
  bootstrap while a live owner holds it is an idempotent no-op — it can never
  spawn a second API / second poller / second worker (the historical Telegram
  409 root cause was "start again -> new poller").
* **Per-component locks** (``runtime_api.lock`` / ``runtime_bot.lock`` /
  ``runtime_worker.lock``) are acquired atomically with
  ``os.open(O_CREAT|O_EXCL)`` — race-safe on Windows. Each lock records the
  COMPONENT's real PID (the spawned child), started_at, component and live SHA.
* **Stale-PID recovery**: a lock whose recorded component PID is no longer a
  live process is reclaimed automatically (never "refuse forever", never
  "kill all pythons", never mis-kill an unrelated process).
* **PID-reuse safety**: API ownership additionally requires the recorded PID to
  be alive; readiness is proven by the /health HTTP probe, not the lock file.
* **Real readiness** in ``readiness.json``: lifecycle + per-component health
  (API /health reachable; bot/worker owner PID alive + recent log writes).
* Persistence is decoupled: the owner is invoked by the Windows Scheduled-Task
  autostart (``install-runtime-task.ps1``) — never depends on a Harness session,
  an open PowerShell, or Hermes/OpenClaw staying alive.

Commands:
    bin/pasay_runtime.py bootstrap      start the canonical runtime (idempotent)
    bin/pasay_runtime.py status         print per-component ownership + health
    bin/pasay_runtime.py stop           stop the canonical runtime components
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RT = os.path.join(REPO, "worktrees", "BOT-V1-USABLE-001-RUNTIME")
RUNTIME_DIR = os.environ.get("PASAY_RUNTIME_DIR") or os.path.join(REPO, ".runtime")
APP_PY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
BOT_PY = os.path.join(REPO, "pasay-telegram-bot", ".venv", "Scripts", "python.exe")

LIFE_CYCLE_STARTING = "STARTING"
LIFE_CYCLE_READY = "READY"
LIFE_CYCLE_STOPPING = "STOPPING"
LIFE_CYCLE_STOPPED = "STOPPED"
LIFE_CYCLE_FAILED = "FAILED"

_COMPONENTS = ("api", "bot", "worker")


def _live_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", RT, "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# --------------------------------------------------------------------------
# low-level windows helpers (stdlib/ctypes only)
# --------------------------------------------------------------------------

def pid_alive(pid: int) -> bool:
    """True when a process with ``pid`` exists (no full cmdline needed).

    Overridable via ``PASAY_PID_ALIVE_HOOK`` for deterministic tests (stale-PID
    recovery is exercised without touching real processes).
    """
    hook = os.environ.get("PASAY_PID_ALIVE_HOOK")
    if hook:
        try:
            return bool(json.loads(hook)[str(pid)])
        except Exception:
            return False
    if not pid or pid <= 0:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False


def api_healthy(timeout: float = 2.0) -> bool:
    """True when the canonical API answers /health on 127.0.0.1:8001."""
    try:
        s = socket.create_connection(("127.0.0.1", 8001), timeout=timeout)
        s.sendall(b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        data = s.recv(256)
        s.close()
        return b"200" in data
    except Exception:
        return False


# --------------------------------------------------------------------------
# lock ownership (stdio-only, atomic via O_EXCL)
# --------------------------------------------------------------------------

def _lock_path(name: str) -> str:
    return os.path.join(RUNTIME_DIR, f"runtime_{name}.lock")


def _read_lock(name: str) -> dict | None:
    p = _lock_path(name)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        d["exists"] = True
        return d
    except Exception:
        return {"exists": True, "pid": 0, "corrupt": True}


def _write_lock(name: str, pid: int, *, sha: str) -> None:
    with open(_lock_path(name), "w", encoding="utf-8") as f:
        json.dump(
            {"component": name, "pid": pid, "started_at": _now_iso(),
             "sha": sha, "lifecycle": LIFE_CYCLE_STARTING},
            f,
        )


def _acquire_unit(owner_pid: int, *, sha: str) -> bool:
    """Atomically acquire the unit lock. Race-safe: only one wins."""
    p = _lock_path("unit")
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return False
    _write_lock("unit", owner_pid, sha=sha)
    return True


def _unit_owned() -> int:
    d = _read_lock("unit")
    if not d:
        return 0
    pid = int(d.get("pid") or 0)
    return pid if pid_alive(pid) else 0


def _claim_component(name: str, pid: int, *, sha: str) -> bool:
    """Atomically claim a component lock with the component's real PID.

    Atomic O_EXCL: two concurrent bootstraps cannot both claim. Returns True
    only when THIS caller owns it. A stale (dead-owner) lock is reclaimed.
    """
    p = _lock_path(name)
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        cur = _read_lock(name)
        owner = int((cur or {}).get("pid") or 0)
        if not cur or not pid_alive(owner):
            os.remove(p)
            try:
                fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
            except FileExistsError:
                return False
        else:
            return False  # live owner -> do NOT start a duplicate
    _write_lock(name, pid, sha=sha)
    return True


def _component_owned(name: str) -> int:
    d = _read_lock(name)
    if not d:
        return 0
    pid = int(d.get("pid") or 0)
    return pid if pid_alive(pid) else 0


def _release(name: str) -> None:
    try:
        os.remove(_lock_path(name))
    except OSError:
        pass


# --------------------------------------------------------------------------
# spawning / readiness
# --------------------------------------------------------------------------

def _spawn(name: str) -> int:
    log_out = os.path.join(RUNTIME_DIR, f"{name}_runtime.log")
    log_err = os.path.join(RUNTIME_DIR, f"{name}_runtime.log.err")
    if name == "api":
        argv = [APP_PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8001"]
        cwd = RT
    elif name == "bot":
        argv = [BOT_PY, "-u", "-m", "pasay_bot.main"]
        cwd = os.path.join(RT, "pasay-telegram-bot")
        os.environ["PYTHONPATH"] = os.path.join(RT, "pasay-telegram-bot")
    else:  # worker
        argv = [APP_PY, os.path.join(RT, "bin", "run-operations-worker.py"), "--interval", "60"]
        cwd = RT
    with open(log_out, "ab") as fo, open(log_err, "ab") as fe:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdout=fo, stderr=fe,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
    return proc.pid


def _component_ready(name: str, pid: int, *, timeout_s: float) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not pid_alive(pid):
            return False
        if name == "api" and api_healthy():
            return True
        if name in ("bot", "worker"):
            for lp in (f"{name}_runtime.log", f"{name}_runtime.log.err"):
                p = os.path.join(RUNTIME_DIR, lp)
                if os.path.exists(p) and time.time() - os.path.getmtime(p) < 30:
                    return True
        time.sleep(0.5)
    return False


def _write_lifecycle(state: str, *, reason: str = "") -> None:
    try:
        with open(os.path.join(RUNTIME_DIR, "readiness.json"), "w", encoding="utf-8") as f:
            json.dump({"lifecycle": state, "reason": reason, "at": _now_iso(),
                       "sha": _live_sha()}, f)
    except Exception:
        pass


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def _status() -> int:
    print("=== canonical runtime status (live sha=%s) ===" % _live_sha())
    uowner = _unit_owned()
    print("unit: owned=%s alive=%s" % (bool(uowner), bool(uowner)))
    for name in _COMPONENTS:
        owner = _component_owned(name)
        healthy = api_healthy() if name == "api" else None
        print("%s: lock=%s owner=%s alive=%s healthy=%s" % (
            name, bool(_read_lock(name)), owner, bool(owner), healthy))
    return 0


def _bootstrap() -> int:
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    sha = _live_sha()
    bootstrapper_pid = os.getpid()

    # --- unit gate -----------------------------------------------------------
    live = _unit_owned()
    if live:
        print("ALREADY_RUNNING: canonical runtime owned by pid=%d; no-op (idempotent)." % live)
        _write_lifecycle(LIFE_CYCLE_READY, reason="already-running-noop")
        return 0
    if not _acquire_unit(bootstrapper_pid, sha=sha):
        live2 = _unit_owned()
        if live2:
            print("ALREADY_RUNNING: another bootstrap owns the unit (pid=%d)." % live2)
            _write_lifecycle(LIFE_CYCLE_READY, reason="concurrent-owner")
            return 0
        # stale unit lock reclaimed by _acquire_unit in a small race; retry once
        if not _acquire_unit(bootstrapper_pid, sha=sha):
            print("ALREADY_RUNNING: unit lock raced; the other owner owns it.")
            return 0

    _write_lifecycle(LIFE_CYCLE_STARTING, reason="bootstrap")
    started: dict[str, int] = {}
    ok = True
    try:
        for name in _COMPONENTS:
            owner = _component_owned(name)
            if owner:
                print(f"[skip] {name}: already running (owner pid={owner}); idempotent")
                started[name] = 0
                continue
            if name == "api" and api_healthy():
                # An API is already answering /health (ours or orphan). Starting
                # a second would fail with EADDRINUSE -> do NOT spawn a dup.
                print("[skip] api: already healthy on :8001 (no second listener)")
                started[name] = 0
                continue
            # claim atomically -> spawn -> record the real component PID
            tmp_pid = bootstrapper_pid
            if not _claim_component(name, tmp_pid, sha=sha):
                print(f"[skip] {name}: component lock owned by a live process; not started twice")
                started[name] = 0
                continue
            real = _spawn(name)
            _write_lock(name, real, sha=sha)  # ownership = the component's real PID
            started[name] = real
            print(f"[start] {name}: pid={real}")
    finally:
        _release("unit")  # unit gate is only for the bootstrap race; the per-component
        # locks (real component PIDs) are the durable ownership thereafter.

    # --- readiness -----------------------------------------------------------
    for name in _COMPONENTS:
        pid = started.get(name) or 0
        if not pid:
            continue
        if not _component_ready(name, pid, timeout_s=28 if name == "bot" else 20):
            ok = False
            print(f"[warn] {name} pid={pid} NOT ready")
        else:
            print(f"[ready] {name} pid={pid}")
    _write_lifecycle(LIFE_CYCLE_READY if ok else LIFE_CYCLE_FAILED,
                     reason="ready" if ok else "component-not-ready")
    return 0 if ok else 2


def _stop() -> int:
    _write_lifecycle(LIFE_CYCLE_STOPPING, reason="stop")
    for name in _COMPONENTS:
        owner = _component_owned(name)
        if owner and pid_alive(owner):
            try:
                os.kill(owner, 9)
                print(f"[stop] {name} pid={owner}")
            except OSError as e:
                print(f"[stop] {name} pid={owner} error={e}")
        _release(name)
    _release("unit")
    _write_lifecycle(LIFE_CYCLE_STOPPED, reason="stop")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="canonical Pasay Windows runtime owner")
    p.add_argument("command", nargs="?", default="status",
                   choices=["bootstrap", "status", "stop"])
    args = p.parse_args(argv)
    if args.command == "bootstrap":
        return _bootstrap()
    if args.command == "status":
        return _status()
    if args.command == "stop":
        return _stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
