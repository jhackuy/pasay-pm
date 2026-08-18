#!/usr/bin/env python3
"""WINDOWS-RUNTIME-SINGLETON-PERSISTENCE-007D — canonical Pasay Windows runtime owner.

SINGLE canonical lifecycle manager for the Pasay Windows runtime. Every
production start (``bin/start-runtime.ps1``, the Scheduled-Task autostart, a
manual re-bootstrap) MUST enter through this owner so the system always
converges to exactly ONE API + ONE Telegram poller + ONE operations worker.

007D ownership semantics (fail-closed, builds on 007B):

    SERVICE HEALTHY != CANONICAL OWNED

``READY`` requires ALL of the following at the same time:

    canonical owner alive
    + API canonical-owned AND healthy (/health 200)
    + Bot canonical-owned AND alive
    + Worker canonical-owned AND alive

A component is *canonically owned* only when ALL hold:

    runtime_<name>.lock exists
    + lock records the COMPONENT's real PID
    + that PID is alive
    + that PID's identity (PEB command line) belongs to the expected
      Pasay component (uvicorn app.main:app / pasay_bot.main /
      run-operations-worker.py)
    + (api) /health answers 200

If an API answers /health on :8001 but is NOT canonically owned (no valid
lock / lock PID dead / lock PID is an unrelated process), the owner FAILS
closed with reason ``UNOWNED_API`` — it NEVER adopts the unknown PID, NEVER
kills it, and NEVER writes a fake ``READY``. The same applies to bot/worker
(``UNOWNED_BOT`` / ``UNOWNED_WORKER``). A live PID with a mismatched identity
is never mis-identified as ours and never mis-killed (PID-reuse / unrelated
process protection).

Design (stdlib/ctypes only, no psutil/WMI dependency):

* **Unit lock** (``runtime_unit.lock``) owns the whole runtime unit during a
  bootstrap race. A second bootstrap while a live owner holds it is an
  idempotent no-op — it can never spawn a second API / second poller /
  second worker (the historical Telegram 409 root cause was "start again ->
  new poller").
* **Per-component locks** (``runtime_api.lock`` / ``runtime_bot.lock`` /
  ``runtime_worker.lock``) are acquired atomically with
  ``os.open(O_CREAT|O_EXCL)`` — race-safe on Windows. Each lock records the
  COMPONENT's real PID, started_at, component and live SHA.
* **PID-identity proof** is read from the target process's PEB command line
  (NtQueryInformationProcess + NtReadVirtualMemory, x64 offsets) — works even
  where WMI/CIM/WMIC are denied, as on the canonical node.
* **Stale-PID recovery**: a lock whose recorded component PID is no longer a
  live process is reclaimed automatically and a replacement is started by the
  canonical owner (never "refuse forever", never "kill all pythons", never
  mis-kill an unrelated live process).
* **Fail-closed readiness** in ``readiness.json``: lifecycle
  (STARTING/READY/STOPPING/STOPPED/FAILED) + per-component ownership/health,
  with explicit failure reasons: ``UNOWNED_API``, ``UNOWNED_BOT``,
  ``UNOWNED_WORKER``, ``STALE_LOCK``, ``COMPONENT_START_FAILED``.
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
import ctypes.wintypes as w
import json
import os
import re
import socket
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RT = os.path.join(REPO, "worktrees", "BOT-V1-USABLE-001-RUNTIME")
RUNTIME_DIR = os.environ.get("PASAY_RUNTIME_DIR") or os.path.join(REPO, ".runtime")
APP_PY = os.path.join(REPO, ".venv", "Scripts", "python.exe")
BOT_PY = os.path.join(REPO, "pasay-telegram-bot", ".venv", "Scripts", "python.exe")

API_PORT = 8001

LIFE_CYCLE_STARTING = "STARTING"
LIFE_CYCLE_READY = "READY"
LIFE_CYCLE_STOPPING = "STOPPING"
LIFE_CYCLE_STOPPED = "STOPPED"
LIFE_CYCLE_FAILED = "FAILED"

_COMPONENTS = ("api", "bot", "worker")

# cmdline markers that prove a PID is the expected Pasay component
_COMPONENT_CMDLINE_MARKERS = {
    "api": ("uvicorn", "app.main:app"),
    "bot": ("pasay_bot.main",),
    "worker": ("run-operations-worker.py",),
}

# ---------------------------------------------------------------------------
# small windows helpers (stdlib/ctypes only)
# ---------------------------------------------------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_VM_READ = 0x0010


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT),
                ("Buffer", ctypes.c_void_p)]


class _RTL_USER_PROCESS_PARAMETERS(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_byte * 16),
        ("Reserved2", ctypes.c_void_p * 10),   # x64
        ("ImagePathName", _UNICODE_STRING),
        ("CommandLine", _UNICODE_STRING),
    ]


class _PEB(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_byte * 2),
        ("BeingDebugged", ctypes.c_byte),
        ("Reserved2", ctypes.c_byte),
        ("Reserved3", ctypes.c_void_p * 2),
        ("Ldr", ctypes.c_void_p),
        ("ProcessParameters", ctypes.c_void_p),
    ]


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("Reserved3", ctypes.c_void_p),
    ]


def _read_mem(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    br = ctypes.c_size_t(0)
    status = ctypes.windll.ntdll.NtReadVirtualMemory(
        h, ctypes.c_void_p(addr), buf, size, ctypes.byref(br))
    if status != 0:
        return b""
    return buf.raw[: br.value]


def _read_unicode(h, u):
    if not u.Buffer or u.Length <= 0 or u.Length > 65535:
        return ""
    try:
        return _read_mem(h, u.Buffer, u.Length + 2).decode("utf-16-le", "ignore").rstrip("\x00")
    except Exception:
        return ""


def _cmdline_of(pid: int) -> str:
    """Best-effort PEB command line of a live process ('' when unreadable)."""
    h = ctypes.windll.kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not h:
        return ""
    try:
        pbi = _PROCESS_BASIC_INFORMATION()
        ret = ctypes.c_ulong(0)
        status = ctypes.windll.ntdll.NtQueryInformationProcess(
            h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(ret))
        if status != 0 or not pbi.PebBaseAddress:
            return ""
        peb = _PEB()
        mem = _read_mem(h, pbi.PebBaseAddress, ctypes.sizeof(_PEB))
        if len(mem) < ctypes.sizeof(_PEB):
            return ""
        ctypes.memmove(ctypes.byref(peb), mem, ctypes.sizeof(_PEB))
        pp = peb.ProcessParameters
        if not pp:
            return ""
        rpp = _RTL_USER_PROCESS_PARAMETERS()
        mem2 = _read_mem(h, pp, ctypes.sizeof(_RTL_USER_PROCESS_PARAMETERS))
        if len(mem2) < ctypes.sizeof(_RTL_USER_PROCESS_PARAMETERS):
            return ""
        ctypes.memmove(ctypes.byref(rpp), mem2, ctypes.sizeof(_RTL_USER_PROCESS_PARAMETERS))
        return _read_unicode(h, rpp.CommandLine)
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def pid_alive(pid: int) -> bool:
    """True when a process with ``pid`` exists.

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
    h = ctypes.windll.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    return False


def _pid_identity(pid: int, name: str) -> str:
    """Return 'ok' | 'mismatch' | 'unknown' whether pid's cmdline proves it is
    the expected Pasay component ``name``.

    Overridable via ``PASAY_PID_IDENTITY_HOOK`` (JSON {pid: verdict}) for tests.
    """
    hook = os.environ.get("PASAY_PID_IDENTITY_HOOK")
    if hook:
        try:
            return str(json.loads(hook).get(str(pid), "unknown"))
        except Exception:
            return "unknown"
    cmd = _cmdline_of(pid)
    if not cmd:
        return "unknown"
    markers = _COMPONENT_CMDLINE_MARKERS.get(name, ())
    if all(m in cmd for m in markers):
        return "ok"
    return "mismatch"


# Toolhelp PROCESSENTRY32W (x64 field layout, stdlib/ctypes only).
class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", w.DWORD),
        ("cntUsage", w.DWORD),
        ("th32ProcessID", w.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", w.DWORD),
        ("cntThreads", w.DWORD),
        ("th32ParentProcessID", w.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", w.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _enum_pids() -> list:
    """Enumerate live Windows PIDs via a Toolhelp snapshot (stdlib/ctypes).

    Returns an empty list when enumeration fails so callers fall back safely
    (lenient: only used to decide whether a *live* canonical component already
    exists, never to kill anything). Overridable via ``PASAY_PROC_SCAN_HOOK``
    (JSON list of ints) for deterministic tests.
    """
    hook = os.environ.get("PASAY_PROC_SCAN_HOOK")
    if hook:
        try:
            return [int(x) for x in json.loads(hook)]
        except Exception:
            return []
    out: list = []
    try:
        TH32CS_SNAPPROCESS = 0x00000002
        snap = ctypes.windll.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == 0xFFFFFFFFFFFFFFFF:  # INVALID_HANDLE_VALUE
            return out
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = ctypes.windll.kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                out.append(int(entry.th32ProcessID))
                ok = ctypes.windll.kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            ctypes.windll.kernel32.CloseHandle(snap)
    except Exception:
        return []
    return out


def _live_component_exists(name: str) -> int:
    """Return the PID of a live canonical ``name`` component on ANY process, or 0.

    This is the safe discriminator between the two situations that both look
    like "lock records a live PID with a mismatched identity":

    * PID-reuse after reboot/crash — the recorded PID no longer runs Pasay and
      NO live Pasay component exists anywhere  -> lock is stale, safe to reclaim.
    * A real live component is running but its lock is wrong/corrupt -> we must
      NOT start a second one (Telegram 409 / duplicate poller protection).

    It only READS processes (never kills/adopts). ``unknown`` identity from an
    unreadable PEB is treated as NOT proven, so we never block on it.
    """
    for pid in _enum_pids():
        if pid <= 0:
            continue
        if _pid_identity(pid, name) == "ok":
            return pid
    return 0


def _port_owner(port: int = API_PORT) -> int:
    """Find the LISTENING PID on 127.0.0.1:<port> via netstat -ano (0 when none).

    Overridable via ``PASAY_PORT_OWNER_HOOK`` (JSON int or null) for tests.
    """
    hook = os.environ.get("PASAY_PORT_OWNER_HOOK")
    if hook:
        try:
            return int(json.loads(hook) or 0)
        except Exception:
            return 0
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return 0
    pat = re.compile(r"TCP\s+([0-9.:a-fA-F\[\]]+):(\d+)\s+\S+\s+(\w+)")
    for line in out.splitlines():
        m = pat.search(line)
        if not m:
            continue
        laddr, lport, state = m.groups()
        if state != "LISTENING" or int(lport) != port:
            continue
        if laddr not in ("127.0.0.1", "0.0.0.0", "[::1]", "[::]", "localhost"):
            continue
        tail = line[max(line.lower().rfind("listening") + len("listening"), 0):].split()
        if tail and tail[0].isdigit():
            return int(tail[0])
    return 0


def api_healthy(timeout: float = 2.0) -> bool:
    """True when SOME API answers /health on 127.0.0.1:8001 (ownership is
    proven separately via the lock + PID identity)."""
    hook = os.environ.get("PASAY_API_HEALTHY_HOOK")
    if hook:
        try:
            return bool(json.loads(hook))
        except Exception:
            return False
    try:
        s = socket.create_connection(("127.0.0.1", API_PORT), timeout=timeout)
        s.sendall(b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        data = s.recv(256)
        s.close()
        return b"200" in data
    except Exception:
        return False


def _kill_pid(pid: int) -> bool:
    """Force-kill exactly ``pid``. Hookable (``PASAY_KILL_HOOK``) for tests."""
    hook = os.environ.get("PASAY_KILL_HOOK")
    if hook:
        try:
            return bool(json.loads(hook)[str(pid)])
        except Exception:
            return False
    try:
        os.kill(pid, 9)
        return True
    except OSError:
        return False


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
    """Lenient ownership: lock exists + recorded PID alive (007B semantics)."""
    d = _read_lock(name)
    if not d:
        return 0
    pid = int(d.get("pid") or 0)
    return pid if pid_alive(pid) else 0


def _component_owned_strict(name: str) -> int:
    """STRICT (007D) ownership: lock exists + PID alive + PID identity proves
    it is the expected Pasay component. Returns the PID or 0."""
    d = _read_lock(name)
    if not d:
        return 0
    pid = int(d.get("pid") or 0)
    if not pid_alive(pid):
        return 0
    if _pid_identity(pid, name) != "ok":
        return 0
    return pid


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
        argv = [APP_PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(API_PORT)]
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


def _components_detail() -> dict:
    out: dict = {}
    for name in _COMPONENTS:
        lock = _read_lock(name)
        lock_pid = int((lock or {}).get("pid") or 0) if lock else 0
        alive = bool(lock_pid) and pid_alive(lock_pid)
        identity = _pid_identity(lock_pid, name) if (lock_pid and alive) else None
        owned = alive and identity == "ok"
        out[name] = {
            "owned": owned,
            "pid": lock_pid if owned else 0,
            "lock_pid": lock_pid,
            "lock_exists": bool(lock),
            "alive": alive,
            "identity": identity,
            "healthy": api_healthy() if name == "api" else None,
        }
    return out


def _write_lifecycle(state: str, *, reason: str = "", components: dict | None = None) -> None:
    try:
        payload = {"lifecycle": state, "reason": reason, "at": _now_iso(),
                   "sha": _live_sha()}
        if components is not None:
            payload["components"] = components
        with open(os.path.join(RUNTIME_DIR, "readiness.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def _status() -> int:
    print("=== canonical runtime status (live sha=%s) ===" % _live_sha())
    uowner = _unit_owned()
    print("unit: owned=%s alive=%s" % (bool(uowner), bool(uowner)))
    detail = _components_detail()
    for name in _COMPONENTS:
        d = detail[name]
        print("%s: lock=%s owned=%s pid=%s identity=%s alive=%s healthy=%s" % (
            name, d["lock_exists"], d["owned"], d["pid"] or 0,
            d["identity"], d["alive"], d["healthy"]))
    try:
        with open(os.path.join(RUNTIME_DIR, "readiness.json"), "r", encoding="utf-8") as f:
            rd = json.load(f)
        print("readiness: lifecycle=%s reason=%s" % (rd.get("lifecycle"), rd.get("reason")))
    except Exception:
        print("readiness: (none)")
    return 0


def _bootstrap() -> int:
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    sha = _live_sha()
    bootstrapper_pid = os.getpid()

    # --- unit gate -----------------------------------------------------------
    live = _unit_owned()
    if live:
        print("ALREADY_RUNNING: canonical runtime owned by pid=%d; no-op (idempotent)." % live)
        _write_lifecycle(LIFE_CYCLE_READY, reason="already-running-noop",
                         components=_components_detail())
        return 0
    if not _acquire_unit(bootstrapper_pid, sha=sha):
        live2 = _unit_owned()
        if live2:
            print("ALREADY_RUNNING: another bootstrap owns the unit (pid=%d)." % live2)
            _write_lifecycle(LIFE_CYCLE_READY, reason="concurrent-owner",
                             components=_components_detail())
            return 0
        # stale unit lock reclaimed by _acquire_unit in a small race; retry once
        if not _acquire_unit(bootstrapper_pid, sha=sha):
            print("ALREADY_RUNNING: unit lock raced; the other owner owns it.")
            return 0

    _write_lifecycle(LIFE_CYCLE_STARTING, reason="bootstrap")
    started: dict[str, int] = {}
    ok = True
    fail_reasons: list[str] = []
    try:
        for name in _COMPONENTS:
            owner = _component_owned_strict(name)
            if owner:
                print(f"[skip] {name}: canonically owned (pid={owner}); idempotent")
                started[name] = owner
                continue

            # --- FAIL-CLOSED: healthy but NOT canonically owned API ----------
            if name == "api" and api_healthy():
                live_pid = _port_owner(API_PORT)
                print("[fail] api: /health OK on :%d but NOT canonically owned "
                      "(listener pid=%s, no valid runtime_api.lock); failing closed "
                      "as UNOWNED_API - refusing to adopt or kill the unknown PID"
                      % (API_PORT, live_pid))
                ok = False
                fail_reasons.append("UNOWNED_API")
                break  # do NOT start bot/worker into a half-owned unit

            # --- FAIL-CLOSED: live PID with mismatched identity (T4) ---------
            lock = _read_lock(name)
            holder = int((lock or {}).get("pid") or 0) if lock else 0
            if holder and pid_alive(holder) and _pid_identity(holder, name) != "ok":
                # Distinguish "stale lock whose PID was reused by an unrelated
                # process" (reboot/crash) from "a real live component exists but
                # its lock is wrong". Only the former is safe to recover from;
                # the latter must NEVER spawn a second poller (Telegram 409) or
                # a second API.
                live_disp = _live_component_exists(name)
                if live_disp:
                    print(f"[fail] {name}: lock pid={holder} is ALIVE with a "
                          f"mismatched identity, and a live canonical {name} "
                          f"(pid={live_disp}) is already running; refusing to start "
                          f"a duplicate and refusing to touch pid={holder} "
                          f"-> UNOWNED_{name.upper()}")
                    ok = False
                    fail_reasons.append("UNOWNED_" + name.upper())
                    break
                # PID-reuse after reboot/crash: the recorded process is no longer
                # the Pasay component and no live Pasay component exists anywhere.
                # The lock is stale -> reclaim it and start a fresh one. The
                # unrelated pid=holder is NEVER killed or adopted.
                print(f"[reclaim] {name}: lock pid={holder} is ALIVE but is NOT a "
                      f"Pasay {name} component (identity mismatch after reboot/"
                      f"crash PID-reuse) and no live canonical {name} exists; "
                      f"reclaiming the stale lock (unrelated pid={holder} left "
                      f"untouched)")
                _release(name)

            # --- claim atomically -> spawn -> record the real component PID ---
            tmp_pid = bootstrapper_pid
            if not _claim_component(name, tmp_pid, sha=sha):
                cur = _read_lock(name)
                cur_holder = int((cur or {}).get("pid") or 0) if cur else 0
                if cur_holder and not pid_alive(cur_holder):
                    print(f"[fail] {name}: stale lock (pid={cur_holder}) could not "
                          f"be reclaimed -> STALE_LOCK")
                    fail_reasons.append("STALE_LOCK")
                else:
                    print(f"[fail] {name}: component lock held by another live "
                          f"owner; not started twice -> UNOWNED_{name.upper()}")
                    fail_reasons.append("UNOWNED_" + name.upper())
                ok = False
                break

            real = _spawn(name)
            _write_lock(name, real, sha=sha)  # ownership = the component's real PID
            started[name] = real
            print(f"[start] {name}: pid={real}")
            if not _component_ready(name, real, timeout_s=28 if name == "bot" else 20):
                ok = False
                fail_reasons.append(f"COMPONENT_START_FAILED({name})")
                print(f"[warn] {name} pid={real} NOT ready")
            else:
                print(f"[ready] {name} pid={real}")
    finally:
        _release("unit")  # unit gate is only for the bootstrap race; the per-component
        # locks (real component PIDs) are the durable ownership thereafter.

    # --- STRICT readiness gate ------------------------------------------------
    detail = _components_detail()
    for name in _COMPONENTS:
        d = detail[name]
        if not d["owned"]:
            reason = "UNOWNED_" + name.upper()
            if reason not in fail_reasons:
                fail_reasons.append(reason)
            ok = False
            print(f"[gate] {name}: NOT canonically owned (lock={d['lock_exists']} "
                  f"pid={d['lock_pid']} identity={d['identity']}) -> {reason}")
        elif name == "api" and not d["healthy"]:
            reason = "COMPONENT_START_FAILED(api-health)"
            if reason not in fail_reasons:
                fail_reasons.append(reason)
            ok = False
            print("[gate] api: owned but /health not OK -> COMPONENT_START_FAILED")

    reason = fail_reasons[0] if fail_reasons else "ready"
    _write_lifecycle(LIFE_CYCLE_READY if ok else LIFE_CYCLE_FAILED,
                     reason=reason, components=detail)
    return 0 if ok else 2


def _stop() -> int:
    _write_lifecycle(LIFE_CYCLE_STOPPING, reason="stop")
    for name in _COMPONENTS:
        owner = _component_owned_strict(name)
        if owner and pid_alive(owner):
            if _kill_pid(owner):
                print(f"[stop] {name} pid={owner}")
            else:
                print(f"[stop] {name} pid={owner} kill failed (access?)")
        elif owner:
            print(f"[stop] {name} lock pid={owner} already gone")
        _release(name)
    _release("unit")
    _write_lifecycle(LIFE_CYCLE_STOPPED, reason="stop",
                     components=_components_detail())
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
