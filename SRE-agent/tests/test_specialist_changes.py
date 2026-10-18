from agent.specialists import build_specialist_findings


def test_just_deployed_release_is_a_recent_change():
    findings = build_specialist_findings({
        "recent_deploys": [{"version": "new", "minutes_ago": 0}],
        "metrics": {"error_rate_percent": 20},
    }, {})
    change = next(item for item in findings if item["specialist"] == "change")
    assert change["status"] == "supporting"
    assert change["candidate_hints"][0]["incident_type"] == "DeploymentRegression"
