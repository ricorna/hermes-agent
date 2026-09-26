"""Execute the attribution workflow against a divergent-base Git history."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


@pytest.mark.skipif(shutil.which("bash") is None or shutil.which("jq") is None,
                    reason="The attribution job requires bash and jq")
@pytest.mark.parametrize("mapped", [True, False])
def test_attribution_checks_only_authors_introduced_after_target(tmp_path, mapped):
    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / ".github/workflows/contributor-check.yml")
        .read_text(encoding="utf-8")
    )
    step = next(s for s in workflow["jobs"]["check-attribution"]["steps"]
                if s.get("id") == "check-emails")
    env = dict(os.environ, GIT_AUTHOR_NAME="Test Author", GIT_COMMITTER_NAME="Test Author",
               GIT_AUTHOR_EMAIL="root@example.test", GIT_COMMITTER_EMAIL="root@example.test")

    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, env=env,
                              capture_output=True, text=True, check=True).stdout.strip()

    git("init")
    git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "root")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    env["GIT_AUTHOR_EMAIL"] = "historical@example.test"
    git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "target history")
    git("update-ref", "refs/remotes/origin/audit/pinned-base", "HEAD")
    env["GIT_AUTHOR_EMAIL"] = "candidate@example.test"
    git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "candidate")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/release.py").write_text("AUTHOR_MAP = {}\n", encoding="utf-8")
    emails = tmp_path / "contributors/emails"
    emails.mkdir(parents=True)
    if mapped:
        (emails / "candidate@example.test").write_text("candidate\n", encoding="utf-8")
    # Resolve the workflow's environment binding as Actions does for this PR.
    bindings = step.get("env", {})
    env.update({key: "audit/pinned-base" if value == "${{ github.base_ref || 'main' }}"
                else str(value) for key, value in bindings.items()})
    env["GITHUB_OUTPUT"] = str(tmp_path / "outputs")
    result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                            cwd=tmp_path, env=env, capture_output=True, text=True)
    assert "historical@example.test" not in result.stdout, result.stdout
    assert result.returncode == (0 if mapped else 1), result.stdout + result.stderr
    if not mapped:
        assert "candidate@example.test" in result.stdout
        assert "action_required" in (tmp_path / "outputs").read_text(encoding="utf-8")
