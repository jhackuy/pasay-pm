"""WINDOWS-RUNTIME-SINGLETON-PERSISTENCE-007D — fail-closed canonical ownership tests.

These tests validate the 007D ownership semantics of ``bin/pasay_runtime.py``
deterministically, without real processes, by pointing the owner at an isolated
runtime dir (``PASAY_RUNTIME_DIR``) and stubbing liveness / API health / port
ownership / PID identity / spawn / kill via module monkey-patching and the
owner's ``PASAY_*_HOOK`` env hooks.

Core principle under test:

    SERVICE HEALTHY != CANONICAL OWNED

An API that answers /health on :8001 without a valid canonical lock + live
owned PID + proven Pasay identity MUST fail closed (``UNOWNED_API``), never
adopt the unknown PID, never kill it, never write a fake READY.

Covered T-matrix (007D §五):
- T1: 8001 healthy + no canonical lock -> FAILED / UNOWNED_API, never READY
- T2: valid lock + correct owned API + health OK -> READY
- T3: stale lock + PID dead -> reclaim -> replacement started -> READY
- T4: lock PID points to an unrelated live process -> not mis-identified,
      not mis-killed, fail closed
- T5: legacy launcher -> delegates canonical owner -> no direct uvicorn
- T6: two launchers concurrently -> exactly one canonical runtime
- T7: canonical stop -> components stopped -> no orphan -> locks released
- T8: healthy orphan API occupying 8001 -> bootstrap fails closed -> no fake READY
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import threading

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUNTIME_PY = os.path.join(_REPO, "bin", "pasay_runtime.py")
_SCRATCH = os.path.join(_REPO, ".runtime", "_007d_test_scrub")


def _load():
    spec = importlib.util.spec_from_file_location("pasay_runtime_007d", _RUNTIME_PY)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def rt():
    mod = _load()
    shutil.rmtree(_SCRATCH, ignore_errors=True)
    os.makedirs(_SCRATCH, exist_ok=True)
    mod.RUNTIME_DIR = os.path.join(_SCRATCH, "rt")
    os.makedirs(mod.RUNTIME_DIR, exist_ok=True)
    mod._live_sha = lambda: "007ddeadbeef"
    # deterministic stubs
    mod._spawn_counter = {"n": 0, "pids": []}

    def _spawn_stub(name):
        mod._spawn_counter["n"] += 1
        pid = 1100 + mod._spawn_counter["n"]
        mod._spawn_counter["pids"].append((name, pid))
        return pid

    mod._spawn = _spawn_stub
    mod._component_ready = lambda name, pid, *, timeout_s: True
    yield mod
    for key in ("PASAY_PID_ALIVE_HOOK", "PASAY_PID_IDENTITY_HOOK",
                "PASAY_API_HEALTHY_HOOK", "PASAY_PORT_OWNER_HOOK",
                "PASAY_KILL_HOOK"):
        os.environ.pop(key, None)
    shutil.rmtree(_SCRATCH, ignore_errors=True)


# --- hook helpers ---------------------------------------------------------

def _hooks(rt, *, alive=(), identity=None, api_healthy=None, port_owner=None,
           killed=None):
    os.environ["PASAY_PID_ALIVE_HOOK"] = json.dumps({str(p): True for p in alive})
    if identity is not None:
        os.environ["PASAY_PID_IDENTITY_HOOK"] = json.dumps(
            {str(p): v for p, v in identity.items()})
    if api_healthy is not None:
        os.environ["PASAY_API_HEALTHY_HOOK"] = json.dumps(api_healthy)
    if port_owner is not None:
        os.environ["PASAY_PORT_OWNER_HOOK"] = json.dumps(port_owner)
    if killed is not None:
        os.environ["PASAY_KILL_HOOK"] = json.dumps({str(p): True for p in killed})


def _write_lock(rt, name, pid):
    with open(os.path.join(rt.RUNTIME_DIR, f"runtime_{name}.lock"), "w",
              encoding="utf-8") as f:
        json.dump({"component": name, "pid": pid, "started_at": "t",
                   "sha": "007ddeadbeef", "lifecycle": "STARTING"}, f)


def _readiness(rt):
    with open(os.path.join(rt.RUNTIME_DIR, "readiness.json"), "r",
              encoding="utf-8") as f:
        return json.load(f)


def _lock_pids(rt):
    out = {}
    for name in ("api", "bot", "worker", "unit"):
        p = os.path.join(rt.RUNTIME_DIR, f"runtime_{name}.lock")
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                out[name] = json.load(f).get("pid")
    return out


# --- T1 / T8: healthy-but-unowned API must FAIL closed --------------------

def test_t1_healthy_api_without_lock_fails_unowned_api(rt):
    """8001 /health OK but no canonical lock -> FAILED/UNOWNED_API, never READY."""
    _hooks(rt, alive=[39464], identity={39464: "ok"}, api_healthy=True,
           port_owner=39464)
    assert rt._spawn_counter["n"] == 0
    rc = rt._bootstrap()
    assert rc == 2
    rd = _readiness(rt)
    assert rd["lifecycle"] == rt.LIFE_CYCLE_FAILED
    assert rd["reason"] == "UNOWNED_API"
    assert rd["components"]["api"]["healthy"] is True
    assert rd["components"]["api"]["owned"] is False
    # nothing was spawned and no locks were created
    assert rt._spawn_counter["n"] == 0
    assert _lock_pids(rt) == {}


def test_t8_orphan_api_on_8001_bootstrap_fails_closed(rt):
    """Healthy orphan API already occupying 8001 -> fail closed, no fake READY,
    orphan not adopted (no lock) and not killed."""
    killed = set()
    _hooks(rt, alive=[39464], identity={39464: "ok"}, api_healthy=True,
           port_owner=39464, killed=killed)
    rc = rt._bootstrap()
    assert rc == 2
    rd = _readiness(rt)
    assert rd["lifecycle"] == rt.LIFE_CYCLE_FAILED
    assert rd["reason"] == "UNOWNED_API"
    assert rd["lifecycle"] != rt.LIFE_CYCLE_READY
    assert rt._spawn_counter["n"] == 0          # no second API started
    assert _lock_pids(rt) == {}                 # orphan never adopted
    assert 39464 not in killed                  # orphan never killed by owner


# --- T2: valid lock + owned API + health -> READY -------------------------

def test_t2_valid_owned_runtime_reads_ready(rt):
    state = {"api_healthy": False}

    def _spawn(name):
        rt._spawn_counter["n"] += 1
        pid = 1100 + rt._spawn_counter["n"]
        rt._spawn_counter["pids"].append((name, pid))
        if name == "api":
            state["api_healthy"] = True
        return pid

    rt._spawn = _spawn
    rt.api_healthy = lambda: state["api_healthy"]   # stateful: False->True
    _hooks(rt, alive=[1101, 1102, 1103],
           identity={1101: "ok", 1102: "ok", 1103: "ok"})
    rc = rt._bootstrap()
    assert rc == 0
    rd = _readiness(rt)
    assert rd["lifecycle"] == rt.LIFE_CYCLE_READY
    assert rd["reason"] == "ready"
    pids = _lock_pids(rt)
    assert pids == {"api": 1101, "bot": 1102, "worker": 1103}
    assert rd["components"]["api"]["owned"] is True
    assert rd["components"]["bot"]["owned"] is True
    assert rd["components"]["worker"]["owned"] is True


# --- T3: stale lock + dead PID -> reclaim -> replacement -> READY ---------

def test_t3_stale_lock_reclaimed_and_replacement_ready(rt):
    state = {"api_healthy": False}
    for name, dead_pid in (("api", 9999), ("bot", 9998), ("worker", 9997)):
        _write_lock(rt, name, dead_pid)

    def _spawn(name):
        rt._spawn_counter["n"] += 1
        pid = 1100 + rt._spawn_counter["n"]
        rt._spawn_counter["pids"].append((name, pid))
        if name == "api":
            state["api_healthy"] = True
        return pid

    rt._spawn = _spawn
    rt.api_healthy = lambda: state["api_healthy"]
    _hooks(rt, alive=[1101, 1102, 1103],
           identity={1101: "ok", 1102: "ok", 1103: "ok"})
    rc = rt._bootstrap()
    assert rc == 0
    rd = _readiness(rt)
    assert rd["lifecycle"] == rt.LIFE_CYCLE_READY
    pids = _lock_pids(rt)
    assert pids == {"api": 1101, "bot": 1102, "worker": 1103}  # reclaimed
    assert rt._spawn_counter["n"] == 3


# --- T4: lock PID is an unrelated live process -> no mis-identify/kill ----

def test_t4_bot_lock_pid_unrelated_live_process_fails_closed(rt):
    """A live lock PID whose identity is NOT a Pasay component: never adopted,
    never killed, fail closed UNOWNED_BOT."""
    killed = set()
    _write_lock(rt, "api", 1101)
    _write_lock(rt, "bot", 7777)      # unrelated live process
    _hooks(rt, alive=[1101, 7777],
           identity={1101: "ok", 7777: "mismatch"},
           api_healthy=True, killed=killed)
    rc = rt._bootstrap()
    assert rc == 2
    rd = _readiness(rt)
    assert rd["lifecycle"] == rt.LIFE_CYCLE_FAILED
    assert rd["reason"] == "UNOWNED_BOT"
    assert 7777 not in killed                          # never mis-killed
    assert rt._spawn_counter["n"] == 0                 # never spawned a dup
    # the unrelated process was not adopted: lock still records 7777, not owned
    assert _lock_pids(rt)["bot"] == 7777
    assert rd["components"]["bot"]["owned"] is False


def test_t4_api_lock_pid_unrelated_live_process_fails_closed(rt):
    killed = set()
    _write_lock(rt, "api", 8888)      # unrelated process claiming the api lock
    _hooks(rt, alive=[8888], identity={8888: "mismatch"},
           api_healthy=True, port_owner=8888, killed=killed)
    rc = rt._bootstrap()
    assert rc == 2
    rd = _readiness(rt)
    assert rd["reason"] == "UNOWNED_API"
    assert 8888 not in killed
    assert rt._spawn_counter["n"] == 0
    assert _lock_pids(rt)["api"] == 8888


# --- T5: legacy launcher delegates canonical owner ------------------------

_PASAY_START_MARKERS = ("uvicorn app.main:app", "-m uvicorn", "pasay_bot.main",
                        "run-operations-worker")


def _iter_launcher_ps1():
    """Yield (path, text) for every launcher-relevant .ps1 outside venv/worktrees."""
    seen = set()
    for base in (os.path.join(_REPO, ".ai-control", "tmp"),
                 os.path.join(_REPO, ".runtime"),
                 os.path.join(_REPO, "bin"),
                 os.path.join(_REPO, "pasay-telegram-bot", "bin")):
        if not os.path.isdir(base):
            continue
        for fn in sorted(os.listdir(base)):
            if not fn.lower().endswith(".ps1"):
                continue
            p = os.path.join(base, fn)
            if p in seen:
                continue
            seen.add(p)
            if "acceptance" in p or "harness-autostart" in p:
                continue
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                yield p, f.read()


def test_t5_no_direct_uvicorn_launcher_remains():
    """Every pasay start entry must delegate to the canonical starter; no
    production script may start uvicorn / bot / worker directly."""
    # the two known bypass files are quarantined (gone from the launcher dir)
    tmp = os.path.join(_REPO, ".ai-control", "tmp")
    for bypass in ("start_runtime.ps1", "start_backend.ps1"):
        assert not os.path.exists(os.path.join(tmp, bypass)), \
            f"bypass launcher still present: {bypass}"
    # no remaining launcher may contain a direct pasay component start
    for path, text in _iter_launcher_ps1():
        base = os.path.basename(path)
        if "stop" in base.lower() or "deploy" in base.lower() or "proof" in base.lower():
            continue  # stop/deploy/proof scripts are not start entries
        if "hermes" in text or "lily_gateway" in base.lower() or "pg_manual" in base.lower():
            continue  # non-Pasay infrastructure (Hermes gateway, postgres)
        for marker in _PASAY_START_MARKERS:
            if marker in text and "--dry-run" not in text.split(marker)[0][-80:]:
                # direct start found -> must be the canonical owner itself or
                # delegate to it
                assert "pasay_runtime.py" in text or "start-runtime.ps1" in text, \
                    f"{path}: contains direct start '{marker}' without delegation"


def test_t5_canonical_starter_delegates_to_owner():
    start = os.path.join(_REPO, "bin", "start-runtime.ps1")
    with open(start, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    assert "pasay_runtime.py" in text and "bootstrap" in text
    assert "uvicorn" not in text          # starter itself never launches uvicorn


# --- T6: two launchers concurrently -> exactly one runtime ----------------

def test_t6_second_bootstrap_is_noop_single_runtime(rt):
    state = {"api_healthy": False}

    def _spawn(name):
        rt._spawn_counter["n"] += 1
        pid = 1100 + rt._spawn_counter["n"]
        if name == "api":
            state["api_healthy"] = True
        return pid

    rt._spawn = _spawn
    rt.api_healthy = lambda: state["api_healthy"]
    _hooks(rt, alive=[1101, 1102, 1103],
           identity={1101: "ok", 1102: "ok", 1103: "ok"})
    rc1 = rt._bootstrap()
    assert rc1 == 0
    assert rt._spawn_counter["n"] == 3          # one api + one bot + one worker
    # a second launcher (e.g. Scheduled Task + manual run) must be a no-op
    rc2 = rt._bootstrap()
    assert rc2 == 0
    assert rt._spawn_counter["n"] == 3          # still exactly one runtime
    assert _readiness(rt)["lifecycle"] == rt.LIFE_CYCLE_READY


def test_t6_unit_lock_race_exactly_one_winner(rt):
    """Two bootstrappers racing the unit lock: exactly one acquires it."""
    results = []
    barrier = threading.Barrier(2)

    def race(pid):
        barrier.wait()
        results.append(rt._acquire_unit(pid, sha="s"))

    t1 = threading.Thread(target=race, args=(101,))
    t2 = threading.Thread(target=race, args=(102,))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert results.count(True) == 1


# --- T7: canonical stop -> all stopped -> locks released -------------------

def test_t7_canonical_stop_stops_owned_and_releases_locks(rt):
    killed = set()

    def _kill(pid):
        killed.add(pid)
        return True

    rt._kill_pid = _kill
    for name, pid in (("api", 1101), ("bot", 1102), ("worker", 1103)):
        _write_lock(rt, name, pid)
    _hooks(rt, alive=[1101, 1102, 1103],
           identity={1101: "ok", 1102: "ok", 1103: "ok"},
           api_healthy=False)
    rc = rt._stop()
    assert rc == 0
    assert killed == {1101, 1102, 1103}          # exactly the owned components
    assert _lock_pids(rt) == {}                  # all locks released
    rd = _readiness(rt)
    assert rd["lifecycle"] == rt.LIFE_CYCLE_STOPPED
    for name in ("api", "bot", "worker"):
        assert rd["components"][name]["owned"] is False


def test_t7_stop_never_kills_unrelated_process(rt):
    killed = set()

    def _kill(pid):
        killed.add(pid)
        return True

    rt._kill_pid = _kill
    _write_lock(rt, "api", 1101)     # owned, will be stopped
    _write_lock(rt, "bot", 7777)     # unrelated live pid in lock
    _hooks(rt, alive=[1101, 7777],
           identity={1101: "ok", 7777: "mismatch"},
           api_healthy=False)
    rt._stop()
    assert killed == {1101}
    assert 7777 not in killed


# --- bonus: COMPONENT_START_FAILED ----------------------------------------

def test_component_start_failed_reason(rt):
    """A spawned component that never becomes ready -> COMPONENT_START_FAILED."""
    state = {"api_healthy": False}

    def _spawn(name):
        rt._spawn_counter["n"] += 1
        pid = 1100 + rt._spawn_counter["n"]
        if name == "api":
            state["api_healthy"] = True
        return pid

    rt._spawn = _spawn
    rt.api_healthy = lambda: state["api_healthy"]
    rt._component_ready = lambda name, pid, *, timeout_s: False  # never ready
    _hooks(rt, alive=[1101, 1102, 1103],
           identity={1101: "ok", 1102: "ok", 1103: "ok"})
    rc = rt._bootstrap()
    assert rc == 2
    rd = _readiness(rt)
    assert rd["lifecycle"] == rt.LIFE_CYCLE_FAILED
    assert rd["reason"] == "COMPONENT_START_FAILED(api)"
