"""A6: triage routes (rollback own release / unsure on human release / pass) with fabricated ledgers."""
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from factory.contracts import Release, RunState
from factory.stages import triage

ALERT = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)
OLD = (ALERT - timedelta(hours=2)).isoformat()


def release(tag, status, promoted_at, commit=None):
    return asdict(Release(tag=tag, commit=commit or f"sha-{tag}", previous_tag="release-0", diff_sha256="d" * 64,
                          run_id="r1", promoted_at=promoted_at, watch_until=promoted_at, status=status,
                          files_touched=["app/cart.py"]))


def run(tmp_path, releases, head=("sha-human", OLD), alert_at=ALERT):
    path = tmp_path / "releases.json"
    path.unlink(missing_ok=True)
    if releases is not None:
        path.write_text(json.dumps({"releases": releases}))
    state = RunState(run_id="r2", alert={"starts_at": alert_at.isoformat()}, artifacts_dir=tmp_path)
    head_fn = head if callable(head) else (lambda: head)
    return state, triage.run(state, now=ALERT, releases_path=path, prod_head=head_fn)


def test_watching_release_routes_to_rollback(tmp_path):
    state, v = run(tmp_path, [release("release-1", "kept", OLD), release("release-2", "watching", OLD)])
    assert (v.status, v.route) == ("fail", "rollback")
    assert v.reasons == ["own release release-2 in watch window"]
    assert isinstance(state.release, Release) and state.release.tag == "release-2"


def test_kept_release_inside_lookback_routes_to_rollback(tmp_path):
    state, v = run(tmp_path, [release("release-1", "kept", (ALERT - timedelta(seconds=5)).isoformat())])
    assert (v.status, v.route, state.release.tag) == ("fail", "rollback", "release-1")


def test_human_release_is_unsure(tmp_path):
    _, v = run(tmp_path, [release("release-1", "kept", OLD)], head=("sha-human", (ALERT - timedelta(seconds=30)).isoformat()))
    assert v.status == "unsure" and v.route is None and v.reasons == ["human release, not reverted"]


def test_pass_cases(tmp_path):
    recent = (ALERT - timedelta(seconds=10)).isoformat()
    # rolled back release, even recent, is not ours to roll back again; head is that release's commit
    _, v = run(tmp_path, [release("release-1", "rolled_back", recent)], head=("sha-release-1", recent))
    assert v.status == "pass" and all(v.checks.values())
    _, v = run(tmp_path, [release("release-1", "rolled_back", OLD)])        # outside window
    assert v.status == "pass"
    _, v = run(tmp_path, [release("release-1", "kept", OLD)], head=("sha-release-1", OLD))  # old kept release
    assert v.status == "pass" and v.route is None
    _, v = run(tmp_path, None)                                              # no ledger file, old prod head
    assert v.status == "pass" and v.evidence["releases"] == 0


def test_unknown_prod_head_is_unsure(tmp_path):
    def broken():
        raise RuntimeError("no git")
    _, v = run(tmp_path, [], head=broken)
    assert v.status == "unsure" and "prod_head_error" in v.evidence


def test_fresh_release_0_tag_is_a_known_release_not_a_human_one(tmp_path, monkeypatch):
    from track_a_support import git, make_mini_repo
    repo = make_mini_repo(tmp_path)                 # prod == release-0, committed just now, not in the ledger
    monkeypatch.setenv("FACTORY_TARGET_SERVICE", str(repo))
    state = RunState(run_id="r3", alert={}, artifacts_dir=tmp_path)
    v = triage.run(state, releases_path=tmp_path / "none.json")
    assert v.status == "pass" and v.evidence["prod_release_tags"] == ["release-0"]
    git(repo, "checkout", "-q", "prod")
    git(repo, "-c", "user.email=h@h", "-c", "user.name=h", "commit", "-q", "--allow-empty", "-m", "hotfix")
    v = triage.run(state, releases_path=tmp_path / "none.json")
    assert v.status == "unsure" and v.reasons == ["human release, not reverted"]
