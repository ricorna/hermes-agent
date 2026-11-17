"""Operator attestation is exact-head evidence, never a hosted-check waiver."""
import argparse
import hashlib
import json
from argparse import Namespace
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pr_acceptance as gate
from hermes_cli.kanban_db_connect import connect

PR = "https://github.com/acme/repo/pull/7"
SHA = "a" * 40
BASE = "c" * 40


@pytest.fixture
def local_case(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    kb.init_db()
    state = {"head": SHA, "required": [], "base": "main", "base_sha": BASE, "status": "open"}

    def api(endpoint, **kwargs):
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": state["head"], "baseRefName": state["base"], "baseRefOid": state["base_sha"], "state": "OPEN",
                "baseRef": {"branchProtectionRule": {"requiredStatusChecks": state["required"]}}}}}}
        if "/rules/branches/" in endpoint:
            return [[]]
        if "/pulls/" in endpoint:
            if state.get("race"):
                state.pop("race")()
            return {"head": {"sha": state["head"]}, "base": {"ref": state["base"], "sha": state["base_sha"]}, "state": state["status"]}
        if "/check-runs" in endpoint:
            return [{"total_count": 0, "check_runs": []}]
        return [[]]

    monkeypatch.setattr(gate, "_api", api)
    evidence = tmp_path / "review.md"
    evidence.write_text(f"Independent reviewer: PASS\nExact head: {SHA}\nExact base: {BASE}\nCommands and results retained.\n")
    with connect() as conn:
        tid = kb.create_task(conn, title="Published task", completion_contract=PR)
    return state, Namespace(task_id=tid, pr=PR, head=SHA, base=BASE, evidence=str(evidence),
                            reason="Operator approved local checks", approval_ref="operator-message-123")


def test_operator_approval_preserves_binding_and_cannot_follow_head(local_case, monkeypatch):
    from hermes_cli import kanban_local_acceptance as admin
    state, args = local_case
    monkeypatch.setattr(admin, "_confirm_operator", lambda *a: None)
    with connect() as conn:
        assert not kb.complete_task(conn, args.task_id, result="local green",
                                    metadata={"local_acceptance": {"ok": True}})
    from hermes_cli import kanban as cli
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    argv = ["kanban", "approve-local-pr", args.task_id]
    for flag in ("pr", "head", "base", "evidence", "reason", "approval_ref"):
        argv += ["--" + flag.replace("_", "-"), getattr(args, flag)]
    assert cli.kanban_command(parser.parse_args(argv)) == 0
    with connect() as conn:
        task = kb.get_task(conn, args.task_id)
        assert task.completion_contract == PR and task.status != "done"
        payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='local_pr_policy'").fetchone()[0])
        assert payload["head_sha"] == SHA and payload["evidence_text"].startswith("Independent")
        assert payload["approval_ref"] == args.approval_ref
        assert payload["base_sha"] == BASE and payload["base_ref"] == "main"
        assert payload["evidence_sha256"] == hashlib.sha256(Path(args.evidence).read_bytes()).hexdigest()
        state["head"] = "b" * 40
        assert not kb.complete_task(conn, args.task_id, result="stale")
        state["head"] = SHA
        state["required"] = [{"context": "ci", "app": None}]
        assert not kb.complete_task(conn, args.task_id, result="missing CI")
        state["required"] = []
        state["base"] = "different-base"
        assert not kb.complete_task(conn, args.task_id, result="changed base")
        state["base"] = "main"
        state["status"] = "closed"
        assert not kb.complete_task(conn, args.task_id, result="closed PR")
        state["status"] = "open"
        assert not kb.complete_task(conn, args.task_id, result="sibling", metadata={"published_pr": PR + "0"})
        assert kb.complete_task(conn, args.task_id, result="reviewed")
        receipt = json.loads(conn.execute("SELECT payload FROM task_events WHERE kind='pr_acceptance' ORDER BY id DESC").fetchone()[0])
        assert receipt["classification"] == "local-reviewed" and receipt["checks"] == []
        assert receipt["base_sha"] == BASE


def test_admin_refuses_unconfirmed_or_changed_task(local_case, monkeypatch):
    from hermes_cli import kanban_local_acceptance as admin
    state, args = local_case
    with pytest.raises(PermissionError):
        admin.approve_local(args)  # no interactive terminal in the runner
    monkeypatch.setattr(admin, "_confirm_operator", lambda *a: None)
    state["head"] = "b" * 40
    with pytest.raises(ValueError):
        admin.approve_local(args)
    state["head"] = SHA

    def reclaim():
        with connect() as conn:
            kb.claim_task(conn, args.task_id)
    state["race"] = reclaim
    with pytest.raises(ValueError):
        admin.approve_local(args)
    with connect() as conn:
        assert conn.execute("SELECT count(*) FROM task_events WHERE kind='local_pr_policy'").fetchone()[0] == 0


def test_worker_and_delegate_cannot_confirm(monkeypatch):
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    from hermes_cli import kanban_local_acceptance as admin
    for marker in ("HERMES_KANBAN_TASK", DELEGATED_CHILD_ENV_MARKER):
        with monkeypatch.context() as scoped:
            scoped.setenv(marker, "worker")
            with pytest.raises(PermissionError, match="workers/delegates"):
                admin._confirm_operator("task", SHA, BASE, "digest")


def test_local_completion_rechecks_ownership(local_case, monkeypatch):
    from hermes_cli import kanban_local_acceptance as admin
    state, args = local_case
    monkeypatch.setattr(admin, "_confirm_operator", lambda *a: None)
    admin.approve_local(args)

    def reclaim():
        with connect() as rival:
            kb.claim_task(rival, args.task_id)
    state["race"] = reclaim
    with connect() as conn:
        assert not kb.complete_task(conn, args.task_id, result="concurrent claim")
        assert kb.get_task(conn, args.task_id).status == "running"
        assert conn.execute("SELECT count(*) FROM task_events WHERE kind='pr_acceptance'").fetchone()[0] == 0


@pytest.mark.parametrize("field,value", [("head", "a" * 39), ("head", "A" * 40),
    ("pr", "https://github.com/acme/repo"), ("reason", " "), ("approval_ref", "")])
def test_invalid_administration_never_confirms(local_case, monkeypatch, field, value):
    from hermes_cli import kanban_local_acceptance as admin
    _, args = local_case
    setattr(args, field, value)
    monkeypatch.setattr(admin, "_confirm_operator", lambda *a: pytest.fail("Invalid input reached confirmation"))
    with pytest.raises(ValueError):
        admin.approve_local(args)


@pytest.mark.parametrize("phase", ["before_admin", "during_admin", "before_completion", "during_completion"])
def test_same_ref_base_advancement_invalidates(local_case, monkeypatch, phase):
    from hermes_cli import kanban_local_acceptance as admin
    state, args = local_case
    monkeypatch.setattr(admin, "_confirm_operator", lambda *a: None)

    def advance():
        state["base_sha"] = "d" * 40

    if phase == "before_admin":
        advance()
    elif phase == "during_admin":
        state["race"] = advance
    if phase.endswith("admin"):
        with pytest.raises(ValueError):
            admin.approve_local(args)
        with connect() as conn:
            assert admin.latest_policy(conn, args.task_id) == (None, None)
        return
    admin.approve_local(args)
    with connect() as conn:
        _, policy = admin.latest_policy(conn, args.task_id)
        assert policy["base_sha"] == BASE
    if phase == "before_completion":
        advance()
    else:
        state["race"] = advance
    with connect() as conn:
        assert not kb.complete_task(conn, args.task_id, result="old base", fire_lifecycle_hook=False)
        assert kb.get_task(conn, args.task_id).status != "done"


@pytest.mark.parametrize("base", [None, "", "c" * 39, "C" * 40])
def test_missing_or_malformed_remote_base_fails_closed(local_case, monkeypatch, base):
    from hermes_cli import kanban_local_acceptance as admin
    state, args = local_case
    monkeypatch.setattr(admin, "_confirm_operator", lambda *a: None)
    state["base_sha"] = base
    with pytest.raises(ValueError):
        admin.approve_local(args)


def test_legacy_policy_without_base_is_not_transferable(local_case, monkeypatch):
    from hermes_cli import kanban_local_acceptance as admin
    from hermes_cli.kanban_db_connect import write_txn
    _, args = local_case
    monkeypatch.setattr(admin, "_confirm_operator", lambda *a: None)
    admin.approve_local(args)
    with connect() as conn:
        _, policy = admin.latest_policy(conn, args.task_id)
        assert policy is not None
        policy.pop("base_sha")
        with write_txn(conn):
            kb._append_event(conn, args.task_id, "local_pr_policy", policy)
        assert not kb.complete_task(conn, args.task_id, result="legacy", fire_lifecycle_hook=False)


def test_api_failure_does_not_use_attestation(local_case, monkeypatch):
    from hermes_cli import kanban_local_acceptance as admin
    _, args = local_case
    monkeypatch.setattr(admin, "_confirm_operator", lambda *a: None)
    admin.approve_local(args)

    def unavailable(*a, **kw):
        raise OSError("offline")
    monkeypatch.setattr(gate, "_api", unavailable)
    with connect() as conn:
        assert not kb.complete_task(conn, args.task_id, result="offline")
        assert kb.get_task(conn, args.task_id).status != "done"
