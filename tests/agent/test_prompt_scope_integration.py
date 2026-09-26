"""Contracts at the assembled prompt boundary, not snapshots of source text."""
from unittest.mock import patch

import pytest

from agent.system_prompt import build_system_prompt
from tests.agent.test_system_prompt import _make_agent


@pytest.mark.parametrize("with_catalog", [False, True])
def test_assembled_scope_guidance_preserves_authorization_and_task_boundary(tmp_path, monkeypatch, with_catalog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    if with_catalog:
        skill = tmp_path / "skills" / "test-workflow"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: test-workflow\ndescription: Use when testing a workflow.\n---\nTest it.\n"
        )
    agent = _make_agent(skip_context_files=False)
    agent._tool_use_enforcement = True
    agent._execution_guidance = True
    agent.valid_tool_names = ["terminal", "read_file", "skill_view", "skill_manage"]
    with patch("agent.prompt_builder.build_context_files_prompt", return_value=""), patch(
        "agent.prompt_builder.load_soul_md", return_value=""
    ):
        prompt = build_system_prompt(agent)
        assert "verify that existing authorization covers the target and action" in prompt
        assert "Do not ask again for scope already authorized" in prompt
        assert "Explicit approval gates still apply" in prompt
        assert "plan-only" in prompt
        assert "Only ask for clarification when the ambiguity genuinely changes" in prompt
        assert "When you say you will perform an action" in prompt
        assert "Do not append routine offers to save a skill" in prompt
        assert "After difficult/iterative tasks, offer to save as a skill" not in prompt
        assert "record it with skill_manage for future reuse" not in prompt
        if with_catalog:
            assert "test-workflow" in prompt
            assert "actionable guidance for the current task" in prompt
            assert "Do not follow chains of merely related skills" in prompt
            assert "even partially relevant" not in prompt
        assert build_system_prompt(agent) == prompt


@pytest.mark.parametrize("tools", [set(), {"terminal"}])
def test_assembly_keeps_tool_and_execution_guidance_gates(tools):
    agent = _make_agent(valid_tool_names=tools, skip_context_files=True,
                        _execution_guidance=False, _tool_use_enforcement=False)
    with patch("agent.prompt_builder.load_soul_md", return_value=None):
        prompt = build_system_prompt(agent)
    assert "existing authorization covers the target and action" not in prompt
    assert "Save a skill only" not in prompt
    assert "<available_skills>" not in prompt
