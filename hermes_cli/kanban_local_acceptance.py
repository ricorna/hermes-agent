"""Operator-only, exact-head local review for PRs with no required hosted checks.

This is an auditable same-OS-user administration surface, not authentication
against arbitrary code running as the database owner. No model tool exposes it.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from hermes_cli import kanban_pr_acceptance as gate
from hermes_cli.kanban_db_connect import connect, write_txn


def latest_policy(conn, task_id):
    row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? "
                       "AND kind='local_pr_policy' ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return (row[0], json.loads(row[1])) if row else (None, None)


def _confirm_operator(task_id, head, base, evidence_digest):
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER
    if os.environ.get("HERMES_KANBAN_TASK") or os.environ.get(DELEGATED_CHILD_ENV_MARKER):
        raise PermissionError("Local PR policy administration is unavailable to workers/delegates")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise PermissionError("Local PR policy requires an interactive operator terminal; no --yes mode")
    expected = f"approve {task_id} {head} {base} {evidence_digest}"
    print("Attest that the retained evidence is an independent PASS for this exact head/base,\n"
          "and the referenced operator explicitly authorized local acceptance.\n"
          "This does not waive required CI, code review, or other completion gates.")
    if input(f"Type exactly: {expected}\n> ") != expected:
        raise PermissionError("Operator confirmation did not match")


def eligible_receipt(contract, head, base, receipt, assignee):
    """Never turn missing/failing required checks or API failures into a local pass."""
    if not (receipt.get("no_required_checks") is True and receipt.get("required") == []
            and receipt.get("head_sha") == head and receipt.get("pr_url") == contract
            and isinstance(base, str) and re.fullmatch(r"[0-9a-f]{40}", base)
            and receipt.get("base_sha") == base):
        return False
    match = gate._PR.fullmatch(contract)
    if match is None:
        return False
    current = gate._api(f"repos/{match[1]}/pulls/{match[2]}",
                        profile_home=gate._assignee_profile_home(assignee))
    return (current["head"]["sha"] == head
            and current["base"]["ref"] == receipt["base_ref"]
            and current["base"]["sha"] == base
            and (current["state"] == "open" or current.get("merged") is True))


def apply_policy(contract, receipt, policy_id, policy, assignee):
    if (policy is None or policy.get("pr_url") != contract
            or policy.get("base_ref") != receipt.get("base_ref")):
        return receipt
    try:
        eligible = eligible_receipt(contract, policy["head_sha"], policy["base_sha"], receipt, assignee)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, gate._GateAuthError):
        eligible = False
    if eligible:
        receipt.update(ok=True, classification="local-reviewed", policy_event_id=policy_id, assignee=assignee,
                       evidence_sha256=policy["evidence_sha256"],
                       detail="Operator-attested independent local review; no required hosted checks configured.")
    return receipt


def approve_local(args):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_pr_acceptance_store import _snapshot

    if (not gate._PR.fullmatch(args.pr) or not re.fullmatch(r"[0-9a-f]{40}", args.head)
            or not re.fullmatch(r"[0-9a-f]{40}", args.base)):
        raise ValueError("Require an exact GitHub PR URL and lowercase 40-character head/base SHAs")
    if not args.reason.strip() or not args.approval_ref.strip():
        raise ValueError("Reason and operator approval reference must be nonempty")
    with Path(args.evidence).open("rb") as stream:
        data = stream.read(65537)
    if not data or len(data) > 65536:
        raise ValueError("Evidence must be nonempty UTF-8 text, at most 64 KiB")
    text = data.decode("utf-8")
    if args.head not in text or args.base not in text:
        raise ValueError("Retained evidence must identify the exact head and base SHAs")
    digest = hashlib.sha256(data).hexdigest()
    with connect() as conn:
        snapshot = _snapshot(conn, args.task_id)
        if snapshot is None or snapshot[2] != args.pr or snapshot[1] not in {"triage", "ready", "blocked", "review"}:
            raise ValueError("Task must be idle/nonterminal and already bound to this exact PR")
        task = kb.get_task(conn, args.task_id)
        if task is None:
            raise ValueError("Task disappeared")
        assignee = task.assignee
        previous_policy_id = latest_policy(conn, args.task_id)[0]
        _confirm_operator(args.task_id, args.head, args.base, digest)
        receipt = gate.collect_acceptance(args.pr, args.pr, assignee=assignee)
        try:
            eligible = eligible_receipt(args.pr, args.head, args.base, receipt, assignee)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, gate._GateAuthError):
            eligible = False
        if not eligible:
            raise ValueError("PR head/base changed, evidence unavailable, or required hosted checks exist")
        payload = {"mode": "local-exact-head", "pr_url": args.pr, "head_sha": args.head,
                   "base_ref": receipt["base_ref"], "base_sha": args.base, "reason": args.reason,
                   "approval_ref": args.approval_ref, "operator": getpass.getuser(),
                   "evidence_path": str(Path(args.evidence).resolve()),
                   "evidence_sha256": digest, "evidence_text": text,
                   "previous_policy_event_id": previous_policy_id}
        with write_txn(conn):
            current = kb.get_task(conn, args.task_id)
            if (current is None or _snapshot(conn, args.task_id) != snapshot
                    or current.assignee != assignee
                    or latest_policy(conn, args.task_id)[0] != previous_policy_id):
                raise ValueError("Task ownership/status/contract changed; retry administration")
            kb._append_event(conn, args.task_id, "local_pr_policy", payload, run_id=snapshot[0])
        print(json.dumps({"task_id": args.task_id, "policy_event_id": latest_policy(conn, args.task_id)[0],
                          "completion_contract": args.pr, "head_sha": args.head, "base_sha": args.base}))
    return 0
