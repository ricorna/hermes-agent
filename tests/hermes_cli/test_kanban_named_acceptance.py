"""Scoped policy must bind jobs, workflow, app, attempt and tested tree."""
import copy
import json
import subprocess

import pytest

from hermes_cli import kanban_pr_acceptance as gate

REPO = "acme/private"
URL = f"https://github.com/{REPO}/pull/3"
HEAD, MERGE, TREE, BLOB = (x * 40 for x in "abcd")
POLICY = {"workflow_id": 12, "workflow_path": ".github/workflows/ci.yml",
          "workflow_blob_sha": BLOB, "app_id": 15368,
          "required_checks": ["tests", "ci ok"], "checkout_checks": ["tests"]}


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps({"kanban": {"pr_acceptance_policies": {REPO: POLICY}}}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    pr = {"number": 3, "state": "open", "merged": False, "merge_commit_sha": MERGE,
          "head": {"sha": HEAD, "repo": {"full_name": REPO}},
          "base": {"sha": "e" * 40, "ref": "main", "repo": {"full_name": REPO}}}
    run = {"id": 42, "run_attempt": 1, "head_sha": HEAD, "event": "pull_request",
           "workflow_id": 12, "path": POLICY["workflow_path"], "check_suite_id": 91,
           "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
           "status": "completed", "conclusion": "success",
           "run_started_at": "2026-09-25T10:00:00Z", "updated_at": "2026-09-25T10:05:00Z",
           "pull_requests": [{"number": 3, "url": f"https://api.github.com/repos/{REPO}/pulls/3",
                              "head": {"sha": HEAD}, "base": {"sha": "e" * 40}}]}
    jobs = [{"id": i, "name": name, "run_id": 42, "run_attempt": 1,
             "head_sha": HEAD, "status": "completed", "conclusion": "success"}
            for i, name in enumerate(POLICY["required_checks"], 100)]
    state = {"pr": pr, "run": run, "jobs": jobs, "tree": TREE, "blob": BLOB,
             "app": 15368, "log": f"2026-09-25T19:00:00Z [command]/usr/bin/git log -1 --format=%H\n2026-09-25T19:00:01Z {MERGE}\n",
             "requests": []}

    def api(endpoint, **kwargs):
        state["requests"].append(endpoint)
        if state.get("error"):
            raise subprocess.CalledProcessError(1, "gh")
        if endpoint.endswith("/pulls/3"):
            value = copy.deepcopy(state["pr"])
            if state.get("race") and state["requests"].count(endpoint) > 1:
                value["head"]["sha"] = "f" * 40
            return value
        if "/contents/" in endpoint:
            return {"sha": evidence_blob if (evidence_blob := state.get("merge_blob")) and endpoint.endswith(MERGE) else state["blob"]}
        if "/workflows/12/runs?" in endpoint:
            runs = [state["run"]]
            if state.get("newer") or (state.get("newer_during_read") and state["requests"].count(endpoint) > 1):
                runs.append({**state["run"], "id": 43, "conclusion": "failure"})
            return [{"total_count": len(runs), "workflow_runs": copy.deepcopy(runs)}]
        if "/attempts/1/jobs?" in endpoint:
            jobs = copy.deepcopy(state["jobs"])
            if state.get("jobs_during_read") and state["requests"].count(endpoint) > 1:
                jobs[0]["conclusion"] = "failure"
            return [{"total_count": len(jobs) + int(bool(state.get("pagination"))), "jobs": jobs}]
        if "/check-runs/" in endpoint:
            job = next(j for j in state["jobs"] if endpoint.endswith(str(j["id"])))
            return {**job, "app": {"id": state["app"]}, "check_suite": {"id": 91}}
        if "/git/commits/" in endpoint:
            sha = endpoint.rsplit("/", 1)[-1]
            return {"sha": sha, "tree": {"sha": state["tree"] if sha == MERGE else TREE},
                    "parents": [{"sha": HEAD}, {"sha": "e" * 40}]}
        if endpoint.endswith("/actions/runs/42"):
            return copy.deepcopy(state["run"])
        raise AssertionError(endpoint)

    monkeypatch.setattr(gate, "_api", api)
    # Deliberately late-bound: the baseline should fail acceptance, not collection.
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 0, state["log"], ""))
    return state


def test_named_policy_accepts_exact_tree_and_records_provenance(evidence):
    result = gate.collect_acceptance(REPO, URL)
    assert result["ok"], result
    assert result["policy_source"] == "explicit_named_checks"
    assert result["tested_sha"] == MERGE
    assert result["head_sha"] == HEAD
    assert result["tree_sha"] == TREE
    assert result["workflow_run_id"] == 42
    assert result["run_attempt"] == 1
    assert len(result["checks"]) == len(POLICY["required_checks"])
    assert not any("rules/branches" in r or r == "graphql" for r in evidence["requests"])


@pytest.mark.parametrize("fault", ["missing", "pending", "failure", "cancelled", "skipped", "neutral", "stale", "tree", "blob", "app", "log", "race", "error", "newer", "attempt", "repo", "workflow", "newer_during_read", "jobs_during_read", "pagination"])
def test_named_policy_rejects_untrusted_or_noncurrent_evidence(evidence, fault):
    if fault == "missing":
        evidence["jobs"].pop()
    elif fault in {"pending", "failure", "cancelled", "skipped", "neutral"}:
        evidence["jobs"][0]["conclusion"] = fault
    elif fault == "stale":
        evidence["jobs"][0]["head_sha"] = "f" * 40
    elif fault in {"tree", "blob"}:
        evidence[fault] = "f" * 40
    elif fault == "app":
        evidence["app"] = 7
    elif fault == "log":
        evidence["log"] = "No checkout evidence"
    elif fault == "attempt":
        evidence["jobs"][0]["run_attempt"] = 2
    elif fault == "repo":
        evidence["pr"]["head"]["repo"]["full_name"] = "other/repo"
    elif fault == "workflow":
        evidence["run"]["workflow_id"] = 99
    else:
        evidence[fault] = True
    assert not gate.collect_acceptance(REPO, URL)["ok"]


def test_direct_head_checkout_is_accepted(evidence):
    evidence["log"] = evidence["log"].replace(MERGE, HEAD)
    assert gate.collect_acceptance(REPO, URL)["ok"]


def test_direct_head_cannot_use_unapproved_merge_workflow(evidence):
    evidence["log"] = evidence["log"].replace(MERGE, HEAD)
    evidence["merge_blob"] = "f" * 40
    assert not gate.collect_acceptance(REPO, URL)["ok"]


def test_duplicate_checkout_evidence_is_rejected(evidence):
    evidence["log"] *= 2
    assert not gate.collect_acceptance(REPO, URL)["ok"]


def test_unconfigured_repository_still_uses_protected_checks(evidence, monkeypatch):
    seen = []
    def unavailable(endpoint, **kwargs):
        seen.append(endpoint)
        raise subprocess.CalledProcessError(1, "gh")
    monkeypatch.setattr(gate, "_api", unavailable)
    result = gate.collect_acceptance("other/repo", "https://github.com/other/repo/pull/3")
    assert not result["ok"]
    assert seen == ["graphql"]
    assert "policy_source" not in result


@pytest.mark.parametrize("value", [[], [""], ["tests", "tests"], "tests", None])
def test_invalid_required_checks_fail_closed(evidence, monkeypatch, value):
    from hermes_cli import config_effective
    policy = {**POLICY, "required_checks": value}
    monkeypatch.setattr(config_effective, "load_user_config_effective", lambda **kw: {"kanban": {"pr_acceptance_policies": {REPO: policy}}})
    assert not gate.collect_acceptance(REPO, URL)["ok"]


def test_policy_receipt_and_terminal_state_are_persisted(evidence):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    kb.init_db()
    with connect() as conn:
        for success in (False, True):
            evidence["jobs"][0]["conclusion"] = "success" if success else "failure"
            task_id = kb.create_task(conn, title="scoped evidence", completion_contract=REPO)
            assert kb.complete_task(conn, task_id, summary="test", metadata={"published_pr": URL}) is success
            assert (kb.get_task(conn, task_id).status == "done") is success
            payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (task_id,)).fetchone()[0])
            assert payload["policy_source"] == "explicit_named_checks"
            assert payload["workflow_run_id"] == 42
            assert payload["ok"] is success


@pytest.mark.parametrize("scenario", ["failed", "pending", "queued_old_time", "racing", "tie", "overlap", "missing_time", "bad_time", "superseded"])
def test_attempt_chronology_not_run_identity(evidence, monkeypatch, scenario):
    other = {**copy.deepcopy(evidence["run"]), "id": 41, "run_attempt": 2,
             "run_started_at": "2026-09-25T11:00:00Z", "updated_at": "2026-09-25T11:05:00Z",
             "conclusion": "failure"}
    if scenario in {"pending", "queued_old_time", "racing"}:
        other.update(status="queued", conclusion=None)
    if scenario in {"superseded", "queued_old_time", "overlap"}:
        other["run_started_at"] = "2026-09-25T09:00:00Z"
        other["updated_at"] = "2026-09-25T09:05:00Z"
    if scenario == "overlap":
        other["updated_at"] = "2026-09-25T10:01:00Z"
    if scenario == "tie":
        other["run_started_at"] = evidence["run"]["run_started_at"]
    if scenario == "missing_time":
        other.pop("run_started_at")
    if scenario == "bad_time":
        other["run_started_at"] = "not a timestamp"
    original_api = gate._api
    reads = 0

    def api(endpoint, **kwargs):
        nonlocal reads
        if "/workflows/12/runs?" in endpoint:
            reads += 1
            runs = [evidence["run"]]
            if scenario != "racing" or reads > 1:
                runs.append(other)
            return [{"total_count": len(runs), "workflow_runs": copy.deepcopy(runs)}]
        return original_api(endpoint, **kwargs)

    monkeypatch.setattr(gate, "_api", api)
    result = gate.collect_acceptance(REPO, URL)
    assert result["ok"] is (scenario == "superseded"), result
    if scenario == "failed":
        assert result["classification"] == "failure"
        assert result["workflow_run_id"] == other["id"]
        assert result["run_attempt"] == other["run_attempt"]


@pytest.fixture
def squashed(evidence, monkeypatch):
    from hermes_cli import kanban_named_acceptance as named
    squash = "1" * 40
    evidence["pr"].update(state="closed", merged=True, merge_commit_sha=squash)
    evidence["run"]["pull_requests"] = []
    stamp = "2026-09-25T19:00:00Z "
    evidence["log"] = (
        stamp + "##[group]Run actions/checkout@" + "2" * 40 + "\n"
        + stamp + f"Syncing repository: {REPO}\n"
        + stamp + f"[command]/usr/bin/git -c protocol.version=2 fetch --no-tags --prune --no-recurse-submodules --depth=1 origin +{MERGE}:refs/remotes/pull/3/merge\n"
        + stamp + "[command]/usr/bin/git checkout --progress --force refs/remotes/pull/3/merge\n"
        + evidence["log"]
        + stamp + "##[group]Run npm test\n"
    )
    original = gate._api
    evidence["witness"] = dict(copy.deepcopy(evidence["run"]), id=77, head_sha=squash,
                               event="push", head_branch="main", check_suite_id=78)
    evidence["witness_jobs"] = [dict(j, id=j["id"] + 100, run_id=77, head_sha=squash)
                                 for j in evidence["jobs"]]
    original_log = named._log
    monkeypatch.setattr(named, "_log", lambda repo, jid: original_log(repo, jid).replace(MERGE, squash)
                        if jid >= 200 else original_log(repo, jid))

    def api(endpoint, **kwargs):
        if "event=push" in endpoint:
            runs = [] if evidence.get("no_witness") else [copy.deepcopy(evidence["witness"])]
            return [{"total_count": len(runs), "workflow_runs": runs}]
        if endpoint.endswith("/actions/runs/77"):
            return copy.deepcopy(evidence["witness"])
        if "/runs/77/attempts/" in endpoint:
            return [{"total_count": len(evidence["witness_jobs"]), "jobs": copy.deepcopy(evidence["witness_jobs"])}]
        if "/check-runs/" in endpoint and int(endpoint.rsplit("/", 1)[1]) >= 200:
            jid = int(endpoint.rsplit("/", 1)[1])
            job = next(j for j in evidence["witness_jobs"] if j["id"] == jid)
            return dict(copy.deepcopy(job), app={"id": 15368}, check_suite={"id": 78})
        if endpoint.endswith("/git/commits/" + squash):
            return {"sha": squash, "tree": {"sha": evidence.get("squash_tree", TREE)},
                    "parents": [{"sha": evidence.get("squash_parent", "e" * 40)}]}
        return original(endpoint, **kwargs)

    monkeypatch.setattr(gate, "_api", api)
    return evidence


@pytest.mark.parametrize("full_history", [False, True])
@pytest.mark.parametrize("empty_metadata", [True, False])
def test_historical_execution_survives_squash(squashed, empty_metadata, full_history):
    if full_history:
        squashed["log"] = squashed["log"].replace("--depth=1 origin +", "--unshallow origin +refs/heads/*:refs/remotes/origin/* +refs/tags/*:refs/tags/* +")
    if not empty_metadata:
        squashed["run"]["pull_requests"] = [{"number": 3,
            "url": f"https://api.github.com/repos/{REPO}/pulls/3",
            "head": {"sha": HEAD}, "base": {"sha": "e" * 40}}]
    result = gate.collect_acceptance(REPO, URL)
    assert result["ok"], result
    assert result["tested_sha"] == result["historical_checkout_sha"] == MERGE
    assert result["workflow_execution_sha"] == squashed["pr"]["merge_commit_sha"]
    assert result["exact_final_execution"]["run_id"] == 77
    assert result["merge_commit_sha"] == squashed["pr"]["merge_commit_sha"]
    assert result["tree_sha"] == TREE


@pytest.mark.parametrize("fault", ["ref", "repo_log", "fetch_sha", "unscoped", "duplicate",
    "squash_tree", "squash_parent", "tree", "blob", "app", "head", "event", "repo",
    "binding", "failed", "attempt", "newer", "jobs_during_read"])
def test_historical_evidence_fails_closed(squashed, fault):
    if fault == "ref":
        squashed["log"] = squashed["log"].replace("pull/3/merge", "pull/4/merge")
    elif fault == "repo_log":
        squashed["log"] = squashed["log"].replace(REPO, "other/repo")
    elif fault == "fetch_sha":
        squashed["log"] = squashed["log"].replace("+" + MERGE, "+" + HEAD)
    elif fault == "unscoped":
        squashed["log"] = squashed["log"].replace("Run actions/checkout@", "Run echo @")
    elif fault == "duplicate":
        squashed["log"] *= 2
    elif fault in {"squash_tree", "squash_parent", "tree", "blob"}:
        squashed[fault] = "f" * 40
    elif fault == "app":
        squashed["app"] = 7
    elif fault == "head":
        squashed["run"]["head_sha"] = "f" * 40
    elif fault == "event":
        squashed["run"]["event"] = "push"
    elif fault == "repo":
        squashed["run"]["repository"]["full_name"] = "other/repo"
    elif fault == "binding":
        squashed["run"]["pull_requests"] = [{"number": 4, "url": "unrelated"}]
    elif fault == "failed":
        squashed["jobs"][0]["conclusion"] = "failure"
    elif fault == "attempt":
        squashed["jobs"][0]["run_attempt"] = 2
    else:
        squashed[fault] = True
    assert not gate.collect_acceptance(REPO, URL)["ok"]


def test_check_rerun_during_collection_is_stale(squashed, monkeypatch):
    original = gate._api
    reads = {}

    def api(endpoint, **kwargs):
        value = original(endpoint, **kwargs)
        if "/check-runs/" in endpoint and int(endpoint.rsplit("/", 1)[1]) < 200:
            reads[endpoint] = reads.get(endpoint, 0) + 1
            if reads[endpoint] > 1:
                value["conclusion"] = "failure"
        return value

    monkeypatch.setattr(gate, "_api", api)
    result = gate.collect_acceptance(REPO, URL)
    assert not result["ok"]
    assert result["classification"] == "stale"


def test_printed_execution_without_independent_witness_is_rejected(squashed):
    squashed["no_witness"] = True
    assert not gate.collect_acceptance(REPO, URL)["ok"]


@pytest.mark.parametrize("field,value", [("head_sha", "f" * 40), ("head_branch", "unrelated"),
    ("event", "pull_request"), ("conclusion", "failure"), ("run_attempt", 2),
    ("workflow_id", 99), ("path", ".github/workflows/other.yml")])
def test_final_execution_witness_must_be_exact(squashed, field, value):
    squashed["witness"][field] = value
    assert not gate.collect_acceptance(REPO, URL)["ok"]


@pytest.mark.parametrize("fault", ["repo", "app", "suite", "failed", "head", "attempt",
    "check_race", "run_race", "missing", "ambiguous", "checkout"])
def test_final_witness_fails_closed(squashed, monkeypatch, fault):
    from hermes_cli import kanban_named_acceptance as named
    original = gate._api
    counts = {}
    if fault == "repo":
        squashed["witness"]["repository"]["full_name"] = "other/repo"
    if fault == "checkout":
        original_log = named._log
        monkeypatch.setattr(named, "_log", lambda repo, jid: original_log(repo, jid).replace("1" * 40, "f" * 40)
                            if jid >= 200 else original_log(repo, jid))

    def api(endpoint, **kwargs):
        value = original(endpoint, **kwargs)
        counts[endpoint] = counts.get(endpoint, 0) + 1
        if "/check-runs/" in endpoint and int(endpoint.rsplit("/", 1)[1]) >= 200:
            if fault == "app": value["app"]["id"] = 1
            if fault == "suite": value["check_suite"]["id"] = 1
            if fault == "failed": value["conclusion"] = "failure"
            if fault == "head": value["head_sha"] = HEAD
            if fault == "check_race" and counts[endpoint] > 1: value["conclusion"] = "failure"
        if "/runs/77/attempts/" in endpoint:
            if fault == "attempt": value[0]["jobs"][0]["run_attempt"] = 2
            if fault == "missing": value[0] = {"total_count": 0, "jobs": []}
        if endpoint.endswith("/runs/77") and fault == "run_race": value["run_attempt"] = 2
        if "event=push" in endpoint and fault == "ambiguous":
            value[0]["workflow_runs"].append(dict(value[0]["workflow_runs"][0], id=79))
            value[0]["total_count"] = 2
        return value

    monkeypatch.setattr(gate, "_api", api)
    assert not gate.collect_acceptance(REPO, URL)["ok"]


def test_empty_policy_fails_closed(evidence, monkeypatch):
    from hermes_cli import config_effective
    monkeypatch.setattr(config_effective, "load_user_config_effective", lambda **kw: {"kanban": {"pr_acceptance_policies": {REPO: {}}}})
    assert not gate.collect_acceptance(REPO, URL)["ok"]
