"""HB-16: `highbar variations` generates a conversation's last turn too.

A frozen run reads the cache only: a multi-turn test whose last turn asks for variations
must find them there, generated with the conversation before that turn.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from highbar.cli.main import app

runner = CliRunner()

SPEC = """\
agent:
  endpoint: "http://localhost:8000/chat"
suites:
  - name: Training
    tests:
      - name: deadline
        turns:
          - trigger: "I want to cancel my training"
          - trigger: "and the deadline?"
            variations: 2
            expect: {response: {contains: "days"}}
"""


class _Response:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self._content}}]}


def test_hb16_the_last_turn_is_generated_with_the_conversation_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "training.highbar.yaml").write_text(
        textwrap.dedent(SPEC), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIGHBAR_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("COLUMNS", "200")
    reply = '["what about the deadline?", "until when?"]'

    with patch("httpx.post", return_value=_Response(reply)) as post:
        result = runner.invoke(app, ["variations"])

    assert result.exit_code == 0, result.output
    assert "1 generated | 0 already cached" in result.output
    prompt = post.call_args.kwargs["json"]["messages"][0]["content"]
    assert "I want to cancel my training" in prompt
    (entry,) = json.loads(
        (tmp_path / ".highbar" / "variations.json").read_text(encoding="utf-8")
    ).values()
    assert entry["trigger"] == "and the deadline?"
    assert entry["context"] == ["I want to cancel my training"]
