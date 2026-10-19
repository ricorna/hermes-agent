"""External evidence is operator-authorized, scoped, and lifecycle fenced."""
import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_external_acceptance as external
from hermes_cli import kanban_pr_acceptance as github
from hermes_cli.kanban_db_connect import connect


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.init_db()
    conn = connect()
    task = kb.create_task(conn, title="Local verified merge", completion_contract="acme/repo")
    run = kb.claim_task(conn, task).current_run_id
    board = external.board_identity(conn)
    archive = tmp_path / "evidence.tar.gz"
    archive.write_bytes(b"fixture: real archive bytes would be reviewed by operator")
    pr_url = "https://github.com/acme/repo/pull/7"
    manifest = {
        "version": 1, "board_db": board, "task_id": task, "run_id": run,
        "repository": "acme/repo", "pr_number": 7, "pr_url": pr_url,
        "head_sha": "a" * 40, "merge_sha": "b" * 40, "tested_sha": "b" * 40,
        "tree_sha": "d" * 40, "artifact_path": str(archive), "artifact_sha256": digest(archive.read_bytes()),
        "review_comment_id": 41, "review_author": "reviewer",
        "review_body_sha256": digest(b"Independent exact-final execution passed."),
        "checks": [{"command": "ctest --output-on-failure", "exit_code": 0}],
    }
    path = tmp_path / "manifest.json"
    config = {"kanban": {"external_evidence_acceptance": {board: {task: {
        "manifest_path": str(path), "manifest_sha256": "",
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }}}}}
    def authorize():
        path.write_text(json.dumps(manifest))
        config["kanban"]["external_evidence_acceptance"][board][task]["manifest_sha256"] = digest(path.read_bytes())
        (home / "config.yaml").write_text(json.dumps(config))
    authorize()
    pr = {"number": 7, "html_url": pr_url, "state": "closed", "merged": True,
          "head": {"sha": "a" * 40}, "base": {"ref": "main", "repo": {"full_name": "acme/repo"}},
          "merge_commit_sha": "b" * 40}
    comment = {"id": 41, "user": {"login": "reviewer"}, "body": "Independent exact-final execution passed.",
               "issue_url": "https://api.github.com/repos/acme/repo/issues/7",
               "html_url": pr_url + "#issuecomment-41"}
    calls = []
    def api(endpoint, **kwargs):
        calls.append(endpoint)
        if endpoint.endswith("/pulls/7"):
            if calls.count(endpoint) == 2 and state.get("stale_pr"):
                pr["head"]["sha"] = "c" * 40
            if state.get("race"):
                callback = state.pop("race")
                callback()
            return copy.deepcopy(pr)
        if endpoint.endswith("/issues/comments/41"):
            if calls.count(endpoint) == 2 and state.get("stale_review"):
                comment["body"] += " edited during read"
            return copy.deepcopy(comment)
        if "/commits/" in endpoint:
            return {"sha": endpoint.rsplit("/", 1)[-1], "commit": {"tree": {
                "sha": "e" * 40 if state.get("changed_merge_tree") and endpoint.endswith("b" * 40) else "d" * 40}}}
        raise AssertionError(f"Unexpected request (no Actions/status writes allowed): {endpoint}")
    monkeypatch.setattr(github, "_api", api)
    state = dict(conn=conn, task=task, run=run, board=board, manifest=manifest, path=path,
                 archive=archive, pr=pr, comment=comment, config=config, home=home,
                 authorize=authorize, calls=calls, url=pr_url)
    yield state
    conn.close()


def complete(e, **metadata):
    return kb.complete_task(e["conn"], e["task"], summary="Independent local execution verified",
                            expected_run_id=e["run"], metadata={"published_pr": e["url"], **metadata})


@pytest.mark.parametrize("tested", ["head_sha", "merge_sha"])
def test_external_evidence_completes_with_auditable_receipt(evidence, tested):
    e = evidence
    e["manifest"]["tested_sha"] = e["manifest"][tested]
    e["authorize"]()
    assert complete(e)
    assert kb.get_task(e["conn"], e["task"]).status == "done"
    receipt = json.loads(e["conn"].execute("SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (e["task"],)).fetchone()[0])
    assert receipt["policy_source"] == "operator_external_evidence"
    assert receipt["artifact_sha256"] == e["manifest"]["artifact_sha256"]
    assert receipt["tested_sha"] == receipt[tested]
    assert receipt["head_sha"] != receipt["merge_sha"]
    assert e["calls"].count("repos/acme/repo/pulls/7") == 2


@pytest.mark.parametrize("field,value", [
    ("board_db", "/another/board.db"), ("task_id", "t_other"), ("run_id", 999999),
    ("repository", "other/repo"), ("pr_number", 8), ("pr_url", "https://github.com/acme/repo/pull/8"),
    ("head_sha", "c" * 40), ("merge_sha", "c" * 40), ("tested_sha", "c" * 40), ("tree_sha", "c" * 40),
    ("artifact_sha256", "0" * 64), ("review_author", "other"), ("review_body_sha256", "0" * 64),
    ("checks", []), ("checks", [{"command": "false", "exit_code": 1}]),
    ("checks", [{"command": "true", "exit_code": False}]), ("version", True),
])
def test_scoping_and_provenance_fail_closed(evidence, field, value):
    e = evidence
    e["manifest"][field] = value
    e["authorize"]()
    assert not complete(e, ok=True, external_evidence={"ok": True})
    assert kb.get_task(e["conn"], e["task"]).status != "done"


@pytest.mark.parametrize("fault", ["tamper", "artifact_missing", "manifest_missing", "expired", "unmerged", "edited_review", "wrong_issue", "bad_config"])
def test_missing_tampered_or_expired_evidence_denied(evidence, fault):
    e = evidence
    def expire():
        e["config"]["kanban"]["external_evidence_acceptance"][e["board"]][e["task"]]["expires_at"] = "2000-01-01T00:00:00Z"
        e["authorize"]()
    mutations = {
        "tamper": lambda: e["path"].write_text(e["path"].read_text() + " "),
        "artifact_missing": e["archive"].unlink,
        "manifest_missing": e["path"].unlink,
        "expired": expire,
        "unmerged": lambda: e["pr"].update(merged=False),
        "edited_review": lambda: e["comment"].update(body=e["comment"]["body"] + " superseded"),
        "wrong_issue": lambda: e["comment"].update(issue_url="https://api.github.com/repos/acme/repo/issues/8"),
        "bad_config": lambda: (e["home"] / "config.yaml").write_text("kanban: {external_evidence_acceptance: []}"),
    }
    mutations[fault]()
    assert not complete(e)


def test_reclaim_race_does_not_publish_receipt_or_complete(evidence):
    e = evidence
    def reclaim():
        assert kb.block_task(e["conn"], e["task"], reason="reassigned")
        assert kb.unblock_task(e["conn"], e["task"])
        assert kb.claim_task(e["conn"], e["task"]).current_run_id != e["run"]
    e["race"] = reclaim
    assert not complete(e)
    assert e["conn"].execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (e["task"],)).fetchone()[0] == 0


def test_policy_revoked_during_collection_denies_terminal_write(evidence):
    e = evidence
    e["race"] = lambda: (e["home"] / "config.yaml").write_text("kanban: {}")
    assert not complete(e)


def test_unconfigured_metadata_cannot_opt_in(evidence, monkeypatch):
    e = evidence
    (e["home"] / "config.yaml").write_text("kanban: {}")
    monkeypatch.setattr(github, "collect_acceptance", lambda *a, **k: {"ok": False, "classification": "missing", "recovery": "retry"})
    from hermes_cli import kanban_pr_acceptance_store as store
    monkeypatch.setattr(store, "collect_acceptance", github.collect_acceptance)
    assert not complete(e, external_evidence={"ok": True, "manifest_path": str(e["path"])})
    assert not e["calls"]


@pytest.mark.parametrize("fault", ["stale_pr", "stale_review", "changed_merge_tree"])
def test_fresh_remote_evidence_is_required(evidence, fault):
    evidence[fault] = True
    assert not complete(evidence)


@pytest.mark.parametrize("error", [OSError("offline"), github._GateAuthError("no credentials")])
def test_api_failure_denies_completion(evidence, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(github, "_api", fail)
    assert not complete(evidence)


@pytest.mark.platforms("posix")
def test_fifo_replacement_does_not_block_acceptance(evidence):
    import os
    evidence["archive"].unlink()
    os.mkfifo(evidence["archive"])
    assert not complete(evidence)


def test_configuration_is_profile_scoped_a_b_a(evidence, tmp_path, monkeypatch):
    e = evidence
    assert external.policy_for(e["board"], e["task"]) is not None
    other = tmp_path / "other-home"
    other.mkdir()
    (other / "config.yaml").write_text("kanban: {}")
    monkeypatch.setenv("HERMES_HOME", str(other))
    assert external.policy_for(e["board"], e["task"]) is None
    monkeypatch.setenv("HERMES_HOME", str(e["home"]))
    assert external.policy_for(e["board"], e["task"]) is not None
    assert external.policy_for(str(other / "board.db"), e["task"]) is None


def test_live_claim_requires_ownership_even_with_approved_evidence(evidence, monkeypatch):
    e = evidence
    monkeypatch.setattr(kb, "_claim_is_live", lambda row: True)
    with pytest.raises(kb.LiveClaimError):
        kb.complete_task(e["conn"], e["task"], summary="not the owner", metadata={"published_pr": e["url"]})
    assert kb.get_task(e["conn"], e["task"]).status == "running"
