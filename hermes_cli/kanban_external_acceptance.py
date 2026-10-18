"""Opt-in operator authorization of independently reviewed, exact-final evidence.

The trust anchor is an operator-pinned manifest digest in profile configuration,
NOT task metadata or a worker's claim of success. This is administrative policy,
not a sandbox against a process able to rewrite config/code/the board database.
No GitHub writes, Actions calls, archive extraction, or evidence execution occur.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from hermes_cli import kanban_pr_acceptance as github

_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_KEYS = {"version", "board_db", "task_id", "run_id", "repository", "pr_number", "pr_url",
         "head_sha", "merge_sha", "tested_sha", "tree_sha", "artifact_path", "artifact_sha256",
         "review_comment_id", "review_author", "review_body_sha256", "checks"}
_RECOVERY = "Ask the operator to review and authorize exact-final evidence for this board/task/run, then retry completion."


def board_identity(conn) -> str:
    """Identity comes from the connected database, never an ambient board env var."""
    path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
    return str(Path(path).resolve()) if path else ""


def policy_for(board: str, task_id: str):
    from hermes_cli.config_effective import load_user_config_effective

    # Broken configuration must never silently revive old authorization.
    try:
        config = load_user_config_effective(fail_closed=True)
    except Exception as exc:
        raise ValueError("External-evidence configuration is unreadable") from exc
    section = config.get("kanban", {})
    if not isinstance(section, dict):
        raise ValueError("Invalid kanban configuration")
    policies = section.get("external_evidence_acceptance", {})
    if not isinstance(policies, dict):
        raise ValueError("Invalid external-evidence policy map")
    tasks = policies.get(board, {})
    if not isinstance(tasks, dict):
        raise ValueError("Invalid board policy map")
    if task_id not in tasks:
        return None
    policy = tasks[task_id]
    if not isinstance(policy, dict) or set(policy) != {"manifest_path", "manifest_sha256", "expires_at"}:
        raise ValueError("Invalid external-evidence policy keys")
    if not isinstance(policy["manifest_path"], str) or not Path(policy["manifest_path"]).is_absolute():
        raise ValueError("Manifest path must be absolute")
    _require_hash(policy["manifest_sha256"], _DIGEST)
    if not isinstance(policy["expires_at"], str):
        raise ValueError("Expiry must be an explicit timestamp")
    expiry = datetime.fromisoformat(policy["expires_at"])
    if expiry.tzinfo is None or expiry <= datetime.now(UTC):
        raise ValueError("External-evidence authorization expired or lacks timezone")
    return policy


def _require_hash(value, pattern):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("Invalid evidence hash")


def _read_bounded(path: str, limit: int) -> bytes:
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError("Evidence path must be absolute")
    # A replaced FIFO must not hang a lifecycle attempt before we can inspect it.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Evidence must be a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Evidence exceeds size budget")
    return data


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest key")
        result[key] = value
    return result


def _manifest(policy, board, task_id, run_id, contract, published_pr):
    raw = _read_bounded(policy["manifest_path"], 65536)
    if hashlib.sha256(raw).hexdigest() != policy["manifest_sha256"]:
        raise ValueError("Manifest differs from operator authorization")
    data = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or set(data) != _KEYS or type(data["version"]) is not int or data["version"] != 1:
        raise ValueError("Unsupported evidence manifest")
    if not board or (data["board_db"], data["task_id"], data["run_id"]) != (board, task_id, run_id):
        raise ValueError("Manifest does not match board/task/run")
    if type(data["run_id"]) is not int or data["run_id"] <= 0:
        raise ValueError("External acceptance requires a claimed run")
    match = github._PR.fullmatch(data["pr_url"]) if isinstance(data["pr_url"], str) else None
    if not match or (match[1], int(match[2])) != (data["repository"], data["pr_number"]):
        raise ValueError("Invalid manifest PR identity")
    if contract not in (data["pr_url"], data["repository"]) or (published_pr and published_pr != data["pr_url"]):
        raise ValueError("Manifest does not match completion contract")
    for key in ("pr_number", "review_comment_id"):
        if type(data[key]) is not int or data[key] <= 0:
            raise ValueError("Invalid GitHub evidence identity")
    for key in ("head_sha", "merge_sha", "tested_sha", "tree_sha"):
        _require_hash(data[key], _SHA)
    for key in ("artifact_sha256", "review_body_sha256"):
        _require_hash(data[key], _DIGEST)
    if data["tested_sha"] not in (data["head_sha"], data["merge_sha"]):
        raise ValueError("Evidence must execute the reviewed head or exact final merge")
    if not isinstance(data["review_author"], str) or not data["review_author"].strip():
        raise ValueError("Missing reviewer identity")
    checks = data["checks"]
    if not isinstance(checks, list) or not checks:
        raise ValueError("Missing independently reviewed checks")
    for check in checks:
        if (not isinstance(check, dict) or set(check) != {"command", "exit_code"}
                or not isinstance(check["command"], str) or not check["command"].strip()
                or type(check["exit_code"]) is not int or check["exit_code"] != 0):
            raise ValueError("External verification did not pass")
    return data


def _pr_identity(pr, data):
    actual = (pr["number"], pr["html_url"], pr["base"]["repo"]["full_name"],
              pr["head"]["sha"], pr["merge_commit_sha"], pr["state"], pr["merged"])
    expected = (data["pr_number"], data["pr_url"], data["repository"],
                data["head_sha"], data["merge_sha"], "closed", True)
    if actual != expected or pr["merged"] is not True:
        raise ValueError("Current PR is not the authorized merged revision")
    return actual + (pr["base"]["ref"],)


def _review_identity(comment, data):
    repo, number = data["repository"], data["pr_number"]
    if (comment["id"] != data["review_comment_id"]
            or comment["user"]["login"] != data["review_author"]
            or comment["issue_url"] != f"https://api.github.com/repos/{repo}/issues/{number}"
            or comment["html_url"] != f"{data['pr_url']}#issuecomment-{data['review_comment_id']}"
            or hashlib.sha256(comment["body"].encode()).hexdigest() != data["review_body_sha256"]):
        raise ValueError("Independent review provenance changed or is unrelated")


def prepare_external(conn, task_id, run_id, contract, published_pr, assignee):
    """None preserves the existing required-check path; explicit policies fail closed."""
    receipt = {"ok": False, "classification": "external_evidence", "head_sha": None,
               "pr_url": published_pr, "checks": [], "recovery": _RECOVERY,
               "policy_source": "operator_external_evidence"}
    try:
        board = board_identity(conn)
        policy = policy_for(board, task_id)
        if policy is None:
            return None
        data = _manifest(policy, board, task_id, run_id, contract, published_pr)
        profile_home = github._assignee_profile_home(assignee)
        def api(endpoint):
            return github._api(endpoint, profile_home=profile_home)
        prefix = f"repos/{data['repository']}"
        pr_endpoint = f"{prefix}/pulls/{data['pr_number']}"
        comment_endpoint = f"{prefix}/issues/comments/{data['review_comment_id']}"
        before = _pr_identity(api(pr_endpoint), data)
        _review_identity(api(comment_endpoint), data)
        # Preserve the tested identity: head execution plus identical merge tree
        # is not represented as an execution of the merge commit itself.
        for sha in {data["head_sha"], data["merge_sha"]}:
            commit = api(f"{prefix}/commits/{sha}")
            if commit["sha"] != sha or commit["commit"]["tree"]["sha"] != data["tree_sha"]:
                raise ValueError("Reviewed head and final merge trees differ")
        archive = _read_bounded(data["artifact_path"], 64 * 1024 * 1024)
        if hashlib.sha256(archive).hexdigest() != data["artifact_sha256"]:
            raise ValueError("Evidence archive differs from independently reviewed bytes")
        _review_identity(api(comment_endpoint), data)
        if _pr_identity(api(pr_endpoint), data) != before:
            raise ValueError("PR changed during collection")
        receipt.update(ok=True, classification="success", pr_url=data["pr_url"],
                       head_sha=data["head_sha"], merge_sha=data["merge_sha"], tested_sha=data["tested_sha"],
                       tree_sha=data["tree_sha"],
                       artifact_sha256=data["artifact_sha256"], checks=data["checks"],
                       review_url=f"{data['pr_url']}#issuecomment-{data['review_comment_id']}",
                       review_author=data["review_author"], review_body_sha256=data["review_body_sha256"],
                       verified_at=datetime.now(UTC).isoformat(),
                       authorization={"board_db": board, "task_id": task_id, "run_id": run_id, "policy": policy})
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, AttributeError, github._GateAuthError):
        # Never persist untrusted exception strings, command stderr, or artifact content.
        receipt["detail"] = "External evidence missing, unauthorized, expired, changed, or unavailable."
    return receipt


def revalidate_external(receipt):
    """Recheck operator authorization/expiry under the existing terminal-write lock."""
    if receipt.get("policy_source") != "operator_external_evidence" or not receipt["ok"]:
        return
    auth = receipt["authorization"]
    try:
        if policy_for(auth["board_db"], auth["task_id"]) == auth["policy"]:
            return
    except (OSError, ValueError, TypeError):
        pass
    receipt.update(ok=False, classification="external_evidence", detail="Operator authorization changed or expired during collection.")
