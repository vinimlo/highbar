"""agent.traces.format (HB-48): highbar's own contract by default, or OTLP JSON lines."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from highbar.engine.parser import parse_spec
from highbar.engine.schema import SpecError

SUITES = """\
suites:
  - name: S
    tests:
      - trigger: "cancel"
        expect: {function_calls: []}
"""


def _spec(tmp_path: Path, agent: str) -> str:
    path = tmp_path / "spec.highbar.yaml"
    body = "agent:\n" + textwrap.indent(textwrap.dedent(agent), "  ") + SUITES
    path.write_text(body, encoding="utf-8")
    return str(path)


ATTACH = 'endpoint: "http://localhost:8000/chat"\n'


def test_the_format_defaults_to_highbar(tmp_path: Path) -> None:
    agent = parse_spec(_spec(tmp_path, ATTACH + 'traces: "var/traces.jsonl"\n')).agent

    assert agent.traces == "var/traces.jsonl"
    assert agent.trace_format == "highbar"


def test_traces_as_a_mapping_names_the_path_and_the_format(tmp_path: Path) -> None:
    agent = parse_spec(
        _spec(tmp_path, ATTACH + "traces:\n  path: var/otel.jsonl\n  format: otlp\n")
    ).agent

    assert agent.traces == "var/otel.jsonl"
    assert agent.trace_format == "otlp"


def test_a_mapping_without_format_is_highbars_contract(tmp_path: Path) -> None:
    agent = parse_spec(_spec(tmp_path, ATTACH + "traces: {path: var/traces.jsonl}\n")).agent

    assert agent.trace_format == "highbar"


def test_an_unknown_format_is_a_spec_error_with_a_suggestion(tmp_path: Path) -> None:
    with pytest.raises(
        SpecError, match=r"agent\.traces\.format: unknown format 'otpl' — did you mean 'otlp'\?"
    ):
        parse_spec(_spec(tmp_path, ATTACH + "traces: {path: var/otel.jsonl, format: otpl}\n"))


def test_an_unknown_key_in_traces_is_a_spec_error(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match=r"agent\.traces: unknown key 'fromat' — did you mean"):
        parse_spec(_spec(tmp_path, ATTACH + "traces: {path: var/otel.jsonl, fromat: otlp}\n"))


def test_a_mapping_needs_a_path(tmp_path: Path) -> None:
    with pytest.raises(SpecError, match=r"agent\.traces: missing 'path'"):
        parse_spec(_spec(tmp_path, ATTACH + "traces: {format: otlp}\n"))


def test_otlp_is_for_attach_mode(tmp_path: Path) -> None:
    managed = 'start: "python agent.py"\n' + ATTACH
    with pytest.raises(SpecError, match="'traces' is for attach mode"):
        parse_spec(_spec(tmp_path, managed + "traces: {path: var/otel.jsonl, format: otlp}\n"))


def test_otlp_correlates_by_trace_id_so_correlate_is_an_error(tmp_path: Path) -> None:
    agent = (
        ATTACH + 'traces: {path: var/otel.jsonl, format: otlp}\ncorrelate: {conversation_id: "1"}\n'
    )
    with pytest.raises(SpecError, match=r"agent\.correlate.*trace id.*traceparent"):
        parse_spec(_spec(tmp_path, agent))


OTLP = "traces:\n  path: var/otel.jsonl\n  format: otlp\n"


def test_tools_lists_the_tools_of_an_otlp_agent(tmp_path: Path) -> None:
    agent = parse_spec(_spec(tmp_path, ATTACH + OTLP + "  tools: [get_order, refund]\n")).agent

    assert agent.trace_tools == ("get_order", "refund")


def test_without_tools_the_list_is_unknown(tmp_path: Path) -> None:
    assert parse_spec(_spec(tmp_path, ATTACH + OTLP)).agent.trace_tools is None


@pytest.mark.parametrize(
    ("tools", "message"),
    [
        ("refund", r"agent\.traces\.tools: must be a list of tool names, got str"),
        ("[refund, 3]", r"agent\.traces\.tools: each tool must be a non-empty string, got int"),
        ('[refund, " "]', r"agent\.traces\.tools: each tool must be a non-empty string"),
        ("[refund, refund]", r"agent\.traces\.tools: 'refund' is listed twice"),
    ],
)
def test_tools_must_be_a_list_of_names(tmp_path: Path, tools: str, message: str) -> None:
    with pytest.raises(SpecError, match=message):
        parse_spec(_spec(tmp_path, ATTACH + OTLP + f"  tools: {tools}\n"))


def test_tools_is_only_for_otlp(tmp_path: Path) -> None:
    agent = ATTACH + "traces: {path: var/traces.jsonl, tools: [refund]}\n"
    with pytest.raises(SpecError, match=r"agent\.traces\.tools: only with format: otlp"):
        parse_spec(_spec(tmp_path, agent))
