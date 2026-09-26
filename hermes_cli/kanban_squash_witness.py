"""Independent exact-final-commit execution witness for historical acceptance."""
import hashlib
from hermes_cli import kanban_named_acceptance as named
from hermes_cli.kanban_named_acceptance import _items, _CHECKOUT


def collect_witness(repo, pr, sha, policy, api):
    """Archived job text alone cannot authenticate the executed workflow.

    Require an additional exact-final push execution of the pinned workflow.
    Unlike a PR run, its authenticated head SHA is its workflow revision.
    Ambiguous multiple executions fail closed rather than selecting old success.
    """
    prefix = f"repos/{repo}"
    endpoint = (f"{prefix}/actions/workflows/{policy['workflow_id']}/runs"
                f"?head_sha={sha}&event=push&per_page=100")
    runs = _items(api(endpoint, paginate=True), "workflow_runs")
    if len(runs) != 1:
        raise ValueError("Missing or ambiguous exact-final execution witness")
    run = runs[0]
    rid, attempt = run["id"], run["run_attempt"]
    if type(rid) is not int or rid <= 0 or type(attempt) is not int or attempt <= 0:
        raise ValueError("Invalid final execution identity")
    if (run["head_sha"] != sha or run["event"] != "push" or
        run["head_branch"] != pr["base"]["ref"] or
        run["repository"]["full_name"] != repo or run["head_repository"]["full_name"] != repo or
        run["workflow_id"] != policy["workflow_id"] or run["path"] != policy["workflow_path"] or
        run["status"] != "completed" or run["conclusion"] != "success"):
        raise ValueError("Untrusted or unsuccessful exact-final execution")
    jobs_url = f"{prefix}/actions/runs/{rid}/attempts/{attempt}/jobs?per_page=100"
    jobs = _items(api(jobs_url, paginate=True), "jobs")
    snapshots, receipts = [], []
    for name in policy["required_checks"]:
        matching = [job for job in jobs if job["name"] == name]
        if len(matching) != 1:
            raise ValueError("Missing or ambiguous final named check")
        job = matching[0]
        if type(job["id"]) is not int or job["id"] <= 0:
            raise ValueError("Invalid final job identity")
        check = api(f"{prefix}/check-runs/{job['id']}")
        if (job["run_id"] != rid or job["run_attempt"] != attempt or job["head_sha"] != sha or
            check["id"] != job["id"] or check["name"] != name or check["head_sha"] != sha or
            check["app"]["id"] != policy["app_id"] or check["check_suite"]["id"] != run["check_suite_id"] or
            any(x["status"] != "completed" or x["conclusion"] != "success" for x in (job, check))):
            raise ValueError("Untrusted or unsuccessful final check")
        snapshots.append(check)
        item = {"name": name, "id": job["id"], "conclusion": "success"}
        if name in policy["checkout_checks"]:
            log = named._log(repo, job["id"])
            if _CHECKOUT.findall(log) != [sha]:
                raise ValueError("Final checkout does not match authenticated execution")
            item.update(tested_sha=sha, log_sha256=hashlib.sha256(log.encode()).hexdigest())
        receipts.append(item)
    if (api(f"{prefix}/actions/runs/{rid}") != run or
        _items(api(endpoint, paginate=True), "workflow_runs") != runs or
        _items(api(jobs_url, paginate=True), "jobs") != jobs or
        any(api(f"{prefix}/check-runs/{check['id']}") != check for check in snapshots)):
        raise ValueError("Final execution evidence changed during collection")
    return {"run_id": rid, "run_attempt": attempt, "event": "push",
            "head_sha": sha, "workflow_execution_sha": sha, "checks": receipts,
            "url": f"https://github.com/{repo}/actions/runs/{rid}/attempts/{attempt}"}
