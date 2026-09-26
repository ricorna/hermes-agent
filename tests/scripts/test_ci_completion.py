"""Execute CI's aggregate shell and prove fork slices cover the discovered suite."""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("result,code", [
    ("success", 0), ("skipped", 0), ("failure", 1),
    ("cancelled", 1), ("timed_out", 1), ("", 1),
])
def test_aggregate_executes_fail_closed(tmp_path, result, code):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yaml").read_text())
    job = workflow["jobs"]["all-checks-pass"]
    step = next(s for s in job["steps"] if s.get("id") == "evaluate")
    output = tmp_path / "output"
    needs = {name: {"result": "success"} for name in job["needs"]}
    needs["tests"]["result"] = result
    env = dict(os.environ, NEEDS=json.dumps(needs), GITHUB_OUTPUT=str(output))
    run = subprocess.run(["bash", "-e", "-c", step["run"]], env=env,
                         text=True, capture_output=True, timeout=15)
    assert run.returncode == code, run.stdout + run.stderr
    compact = json.loads(output.read_text().split("=", 1)[1])
    assert compact == {name: info["result"] for name, info in needs.items()}


def test_fork_workflow_slices_execute_and_partition_entire_suite(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/tests.yml").read_text())
    job = workflow["jobs"]["test"]
    assert job["strategy"]["fail-fast"] is False
    # Evaluate the literal fallback matrix data, not a second hard-coded list.
    slices = json.loads(re.findall(r"'([^']+)'", job["strategy"]["matrix"]["slice"])[-1])
    step = next(s for s in job["steps"] if s.get("name") == "Run tests")
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / ".venv/bin/activate").touch()
    (tmp_path / "scripts").mkdir()
    stub = tmp_path / "scripts/run_tests.sh"
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    stub.chmod(0o755)
    nproc = tmp_path / "nproc"
    nproc.write_text('#!/bin/sh\nprintf "2\\n"\n')
    nproc.chmod(0o755)
    spec = importlib.util.spec_from_file_location("ci_test_runner", ROOT / "scripts/run_tests_parallel.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    files = runner._discover_files([ROOT / "tests"])
    assigned = []
    for value in slices:
        env = dict(os.environ, TEST_SLICE=value, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"])
        run = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path,
                             env=env, text=True, capture_output=True, timeout=15)
        assert run.returncode == 0, run.stderr
        flag, selected = run.stdout.splitlines()
        assert flag == "--slice"
        index, count = map(int, selected.split("/"))
        assigned.extend(runner._slice_files(files, index, count, {}, ROOT))
    assert files
    assert len(assigned) == len(set(assigned)) == len(files)
    assert set(assigned) == set(files)
