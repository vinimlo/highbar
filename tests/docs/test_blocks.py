"""The docs test cannot be fooled by how a block is written (PR 8 review, I3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.docs.blocks import blocks, spec_for


def _one(tmp_path: Path, markdown: str):  # type: ignore[no-untyped-def]
    page = tmp_path / "page.md"
    page.write_text(markdown, encoding="utf-8")
    (block,) = blocks(page)
    return block


SPEC = "agent: {endpoint: 'http://x/chat'}\nsuites: [{name: S, tests: [{trigger: hi}]}]\n"


class TestExtraction:
    @pytest.mark.parametrize("fence", ["```yaml{3}", "```YAML", "```yaml [spec.highbar.yaml]"])
    def test_i3_fence_attributes_and_case_do_not_hide_a_yaml_block(
        self, tmp_path: Path, fence: str
    ) -> None:
        assert _one(tmp_path, f"{fence}\n{SPEC}```\n").lang == "yaml"

    def test_i3_a_block_inside_a_blockquote_is_extracted(self, tmp_path: Path) -> None:
        block = _one(tmp_path, "> Note:\n>\n> ```yaml\n> agent: {}\n> ```\n")
        assert (block.lang, block.text) == ("yaml", "agent: {}\n")


class TestRoles:
    def test_i3_an_agent_block_holds_only_the_agent(self, tmp_path: Path) -> None:
        block = _one(tmp_path, f"```yaml agent\n{SPEC}```\n")
        with pytest.raises(ValueError, match="only the agent"):
            spec_for(block)

    def test_i3_a_workflow_block_cannot_hold_a_spec(self, tmp_path: Path) -> None:
        block = _one(tmp_path, f"```yaml workflow\n{SPEC}```\n")
        with pytest.raises(ValueError, match="spec"):
            spec_for(block)

    def test_an_undeclared_fragment_is_refused(self, tmp_path: Path) -> None:
        block = _one(tmp_path, "```yaml\ntrigger: hi\n```\n")
        with pytest.raises(ValueError, match="declare"):
            spec_for(block)

    def test_a_whole_spec_is_validated_as_written(self, tmp_path: Path) -> None:
        assert spec_for(_one(tmp_path, f"```yaml\n{SPEC}```\n")) == SPEC

    def test_a_workflow_is_not_a_spec(self, tmp_path: Path) -> None:
        block = _one(tmp_path, "```yaml workflow\non: push\njobs: {}\n```\n")
        assert spec_for(block) is None
