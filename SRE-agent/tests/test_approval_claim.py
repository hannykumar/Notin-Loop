from concurrent.futures import ThreadPoolExecutor

from runtime.db import init_db, reset_db_state
from runtime.settings import reset_settings_cache
from runtime.storage import create_run, get_run, mark_approval, update_plan_result


def test_only_one_concurrent_approval_can_claim_execution(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'approval.db'}")
    reset_settings_cache()
    reset_db_state()
    try:
        init_db()
        create_run("race", {"incident_id": "INC002", "execution_mode": "preview", "tool_mode": "mock"})
        update_plan_result("race", {
            "requires_human_approval": True,
            "proposed_action": {"action_type": "scale_deployment", "target": "payments", "replicas": 1},
        })

        def approve(mode):
            try:
                mark_approval("race", "tester", execution_mode=mode)
                return mode
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [result for result in pool.map(approve, ["preview", "simulate"]) if result]
        assert len(winners) == 1
        assert get_run("race")["request"]["execution_mode"] == winners[0]
    finally:
        reset_db_state()
        reset_settings_cache()
