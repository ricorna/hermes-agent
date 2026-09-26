"""SKILLS_GUIDANCE must not carry the phrasing Anthropic's content filter rejects.

#82154: on a subscription OAuth credential, Anthropic's server-side content
filter rejected the first sentence of the built-in ``SKILLS_GUIDANCE`` prompt
and surfaced the rejection as ``HTTP 400 "You're out of extra usage."`` —
a billing-shaped message that sent users to buy quota they did not need.

Bisected against the live API against the full 71,721-char assembled prompt:
that sentence alone reproduced the 400, and removing it alone cleared it.
Size (20 KB of filler → 200) and the ``system[0]`` identity gate (a 429, not a
400) were both ruled out.

These tests pin the reword. They deliberately assert on the *trigger substrings*
rather than on an exact replacement string, so a future rewording is free to
change the prose as long as it does not reintroduce the rejected phrasing or
drop the behaviour the sentence exists to produce.
"""

from __future__ import annotations


import pytest

from agent.prompt_builder import SKILLS_GUIDANCE


# Substrings unique to the rejected sentence. The bisect showed the trigger
# survives removal of the "(5+ tool calls)" clause, so the clause alone is not
# a sufficient guard — the surrounding phrasing is pinned too.
REJECTED_FRAGMENTS = (
    "After completing a complex task",
    "5+ tool calls",
    "fixing a tricky error",
    "save the approach as a",
    "so you can reuse it next time",
)


class TestRejectedPhrasingIsGone:
    @pytest.mark.parametrize("fragment", REJECTED_FRAGMENTS)
    def test_trigger_fragment_absent(self, fragment):
        assert fragment not in SKILLS_GUIDANCE, (
            f"{fragment!r} is part of the phrasing Anthropic's content filter "
            "rejects on subscription OAuth tokens (#82154)"
        )

    def test_save_eligibility_requires_verified_reusable_learning(self):
        # Difficulty is not permission to write. Preserve the scoped policy,
        # without freezing one vendor-tested sentence as the only valid prose.
        first_line = SKILLS_GUIDANCE.split("\n", 1)[0]
        for qualifier in ("verified", "reusable", "not already captured"):
            assert qualifier in first_line
        assert "task difficulty alone is not a reason to write" in first_line


class TestBehaviourIsPreserved:
    """Writing stays subordinate to the deliverable and explicit authority."""

    def test_learning_does_not_displace_the_requested_deliverable(self):
        assert "Do not append routine offers to save a skill" in SKILLS_GUIDANCE
        assert "repeat a completed save" in SKILLS_GUIDANCE
        assert "delay the requested deliverable" in SKILLS_GUIDANCE
        assert "Honor plan-only and no-save requests" in SKILLS_GUIDANCE
        assert "preserve explicit approval gates" in SKILLS_GUIDANCE

    def test_existing_skills_are_patched_before_creating_duplicates(self):
        assert "Patch a relevant existing skill before creating one" in SKILLS_GUIDANCE

    def test_skill_safety_rule_block_untouched(self):
        # Guarded independently by tests/agent/test_ghost_skill_pruning.py; asserted
        # here too so a reword of the guidance can't quietly take the block with it.
        assert "## Skill Safety Rule" in SKILLS_GUIDANCE
        assert "[SKILL_PRUNED]" in SKILLS_GUIDANCE
        for phrase in ("skill_view(name=", "historical artifacts"):
            assert phrase in SKILLS_GUIDANCE

    def test_real_newlines_preserved(self):
        """The block must contain REAL newlines (not escaped backslash-n
        literals) so the safety-rule heading renders as a heading."""
        assert chr(10) in SKILLS_GUIDANCE
        assert (chr(92) + 'n') not in SKILLS_GUIDANCE


class TestGuidanceReachesTheSystemPrompt:
    def test_guidance_is_wired_into_tool_guidance(self):
        # A reword is worthless if the constant stopped being emitted. Assert the
        # wiring behaviorally rather than trusting the constant in isolation.
        from types import SimpleNamespace

        import agent.system_prompt as system_prompt

        agent = SimpleNamespace(valid_tool_names={"skill_manage"}, _kanban_worker_guidance="")
        assert SKILLS_GUIDANCE in (system_prompt._tool_guidance_block(agent) or "")
        agent.valid_tool_names = {"terminal"}
        assert SKILLS_GUIDANCE not in (system_prompt._tool_guidance_block(agent) or "")
