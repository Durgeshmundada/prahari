import importlib.util
import sys
import threading
import time
from pathlib import Path


def _load_action_agent(project_root: Path, tmp_path: Path, monkeypatch):
    token_file = tmp_path / "action-token"
    token_file.write_text("a" * 64, encoding="utf-8")
    monkeypatch.setenv("PRAHARI_ACTION_AGENT_TOKEN_FILE", str(token_file))
    path = project_root / "network-lab" / "scripts" / "action-agent.py"
    spec = importlib.util.spec_from_file_location("prahari_test_action_agent", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_backup_neighbor_state_must_match_the_target_neighbor(
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = _load_action_agent(project_root, tmp_path, monkeypatch)
    output = (
        "Neighbor ID Pri State Dead Time Address Interface RXmtL RqstL DBsmL\n"
        "10.255.0.5 1 Init/DROther 32.1s 10.20.0.2 eth2:10.20.0.1 0 0 0\n"
        "10.255.0.2 1 Full/DR 33.1s 10.10.0.2 eth1:10.10.0.1 0 0 0\n"
    )

    assert agent._neighbor_is_full(output, "10.20.0.2") is False
    assert agent._neighbor_is_full(output, "10.10.0.2") is True
    assert agent._neighbor_is_full(output, "10.255.0.2") is False
    assert agent._neighbor_is_full(output, "10.10.0.20") is False
    with_uptime = "10.255.0.5 1 Full/DROther 01:02:03 32.1s 10.20.0.2 eth2:10.20.0.1 0 0 0"
    assert agent._neighbor_is_full(with_uptime, "10.20.0.2") is True


def test_route_mutations_are_serialized(
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = _load_action_agent(project_root, tmp_path, monkeypatch)
    active = 0
    maximum_active = 0
    counter_lock = threading.Lock()

    def set_primary_cost(_cost: str) -> None:
        nonlocal active, maximum_active
        with counter_lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.03)
        with counter_lock:
            active -= 1

    monkeypatch.setattr(agent, "require_lab", lambda: None)
    monkeypatch.setattr(agent, "set_primary_cost", set_primary_cost)
    monkeypatch.setattr(agent, "wait_for_route", lambda _expected: None)
    monkeypatch.setattr(agent, "ping", lambda: None)
    request = {
        "operation_id": "rollback-agent-test",
        "incident_id": agent.INCIDENT_ID,
        "playbook_id": agent.PLAYBOOK_ID,
        "actor_id": "operator-01",
        "prediction_id": "pred-agent-test",
    }
    threads = [threading.Thread(target=agent.rollback, args=(request,)) for _ in range(2)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert maximum_active == 1


def test_old_prediction_dry_run_cannot_authorize_new_prediction(
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent = _load_action_agent(project_root, tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "require_lab", lambda: None)
    monkeypatch.setattr(agent, "route", lambda: "primary")
    monkeypatch.setattr(agent, "require_backup_neighbors", lambda: None)
    monkeypatch.setattr(agent, "ping", lambda: None)
    changes = []
    monkeypatch.setattr(agent, "set_primary_cost", changes.append)
    monkeypatch.setattr(agent, "wait_for_route", lambda _route: None)
    request = {
        "operation_id": "dry-run-agent-test",
        "incident_id": agent.INCIDENT_ID,
        "playbook_id": agent.PLAYBOOK_ID,
        "actor_id": "operator-01",
        "prediction_id": "pred-old",
    }
    dry_run_result = agent.dry_run(request)
    assert dry_run_result["status"] == "dry_run_passed"
    assert dry_run_result["operation_id"] == request["operation_id"]
    assert agent.apply(
        {**request, "operation_id": "apply-new-prediction", "prediction_id": "pred-new"}
    )["status"] == "blocked"
    assert changes == []
    apply_request = {**request, "operation_id": "apply-agent-test"}
    assert agent.apply(apply_request)["status"] == "applied"
    assert changes == [agent.SHIFT_COST]
    assert agent.apply({**request, "operation_id": "apply-repeat-test"})["status"] == "blocked"
