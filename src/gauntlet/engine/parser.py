"""YAML spec parser — converts .gauntlet.yaml files to domain models."""

from __future__ import annotations

from typing import Any

import yaml

from gauntlet.assertions._match import validate_expected
from gauntlet.domain.models import (
    AgentConfig,
    Expect,
    FunctionCallExpect,
    ResponseExpect,
    Spec,
    Suite,
    Test,
    Turn,
)


def parse_spec(path: str) -> Spec:
    """Parse a .gauntlet.yaml file into a Spec model."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "agent" not in raw:
        msg = "Spec must have an 'agent' block"
        raise ValueError(msg)

    agent = _parse_agent(raw["agent"])
    suites = [_parse_suite(s) for s in raw.get("suites", [])]

    return Spec(agent=agent, suites=suites)


def _parse_agent(raw: dict[str, Any]) -> AgentConfig:
    if "start" not in raw:
        msg = "Agent config must have 'start' command"
        raise ValueError(msg)
    if "endpoint" not in raw:
        msg = "Agent config must have 'endpoint' URL"
        raise ValueError(msg)

    return AgentConfig(
        start=raw["start"],
        endpoint=raw["endpoint"],
        health=raw.get("health"),
        startup_timeout=raw.get("startup_timeout", 15000),
        settle_time=raw.get("settle_time", 500),
    )


def _parse_suite(raw: dict[str, Any]) -> Suite:
    name = raw.get("name", "Unnamed Suite")
    tests = [_parse_test(t) for t in raw.get("tests", [])]
    return Suite(name=name, tests=tests)


def _parse_test(raw: dict[str, Any]) -> Test:
    if "turns" in raw:
        if raw.get("variations"):
            # Paraphrasing turn 1 would leave the later turns answering a conversation
            # that no longer happened. Multi-turn variations need a design of their own.
            msg = "'variations' is not supported on multi-turn tests"
            raise ValueError(msg)
        turns = [_parse_turn(t) for t in raw["turns"]]
        return Test(turns=turns)

    trigger = raw.get("trigger", "")
    expect = _parse_expect(raw.get("expect", {}))
    variations = _parse_variations(raw.get("variations"))
    if variations and not (isinstance(trigger, str) and trigger.strip()):
        # Nothing to paraphrase: `gauntlet variations` would skip it and the run would
        # then miss the cache for an empty sentence.
        msg = "A test with 'variations' must have a non-empty 'trigger'"
        raise ValueError(msg)
    return Test(trigger=trigger, expect=expect, variations=variations)


def _parse_variations(raw: Any) -> int:
    if raw is None:
        return 0
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        msg = f"'variations' must be a non-negative integer, got {raw!r}"
        raise ValueError(msg)
    return raw


def _parse_turn(raw: dict[str, Any]) -> Turn:
    trigger = raw.get("trigger", "")
    expect = _parse_expect(raw.get("expect", {}))
    return Turn(trigger=trigger, expect=expect)


def _parse_expect(raw: dict[str, Any]) -> Expect:
    function_calls = None
    if "function_calls" in raw:
        fc_raw = raw["function_calls"]
        if isinstance(fc_raw, list) and len(fc_raw) == 0:
            function_calls = []
        elif isinstance(fc_raw, list):
            function_calls = [_parse_function_call(fc) for fc in fc_raw]

    response = None
    if "response" in raw:
        r = raw["response"]
        if r is not None:
            response = ResponseExpect(
                contains=r.get("contains"),
                not_contains=r.get("not_contains"),
                matches=r.get("matches"),
            )

    return Expect(function_calls=function_calls, response=response)


def _parse_function_call(raw: dict[str, Any]) -> FunctionCallExpect:
    # A malformed matcher must fail at `gauntlet validate`, not in the middle of a run.
    validate_expected(raw.get("params"), "params")
    validate_expected(raw.get("result"), "result")
    if "name" not in raw:
        raise ValueError("function_call entry must have a 'name' field")

    return FunctionCallExpect(
        name=_parse_call_name(raw["name"]),
        params=raw.get("params"),
        result=raw.get("result"),
    )


def _parse_call_name(raw: Any) -> str | list[str]:
    """Accept a tool name, or a list of tools of which any one satisfies the expectation.

    Validated here rather than left to the assertion: a spec that says
    ``name: [a, 3]`` or ``name: ""`` should fail loudly at `gauntlet validate`, not
    silently never match at run time.
    """
    if isinstance(raw, str):
        if not raw.strip():
            msg = "function_call 'name' cannot be empty"
            raise ValueError(msg)
        return raw

    if isinstance(raw, list):
        if not raw:
            msg = "function_call 'name' cannot be an empty list"
            raise ValueError(msg)
        if not all(isinstance(name, str) and name.strip() for name in raw):
            msg = f"function_call 'name' list must contain only non-empty strings, got {raw!r}"
            raise ValueError(msg)
        # Listing a tool twice adds nothing, and would repeat it in failure messages.
        return list(dict.fromkeys(raw))

    msg = f"function_call 'name' must be a string or a list of strings, got {type(raw).__name__}"
    raise ValueError(msg)
