import importlib.util
import pathlib


MODULE_PATH = pathlib.Path(__file__).parents[1] / "watchdog.py"
SPEC = importlib.util.spec_from_file_location("pasay_watchdog", MODULE_PATH)
assert SPEC and SPEC.loader
watchdog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watchdog)


def test_parse_json_output_ignores_cli_prefix():
    value = watchdog.parse_json_output('AgentTeams log\n{"projects": [{"project_id": "p1"}]}\n')
    assert value["projects"][0]["project_id"] == "p1"


def test_fingerprint_is_stable_across_node_order():
    first = {
        "status": "active",
        "nodes": [
            {"id": "b", "status": "pending", "assignee": "qa"},
            {"id": "a", "status": "completed", "assignee": "dev"},
        ],
        "next": ["b"],
        "interrupts": [],
        "loop": None,
    }
    second = {**first, "nodes": list(reversed(first["nodes"]))}
    assert watchdog.progress_fingerprint(first) == watchdog.progress_fingerprint(second)


def test_stall_count_resets_on_real_progress():
    previous = {"fingerprint": "old", "stall_count": 5}
    assert watchdog.next_stall_count(previous, "old") == 6
    assert watchdog.next_stall_count(previous, "new") == 1


def test_project_list_accepts_nested_connector_shape():
    payload = {"result": {"projects": [{"project_id": "p1"}]}}
    assert watchdog.project_list(payload) == [{"project_id": "p1"}]


def test_hard_brake_targets_only_pasay_workers():
    assert len(watchdog.TEAM_WORKERS) == 6
    assert all(name.startswith("pasay-") for name in watchdog.TEAM_WORKERS)
    assert "manager" not in watchdog.TEAM_WORKERS
