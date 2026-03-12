"""YAML spec parser — converts .gauntlet.yaml files to domain models."""

from __future__ import annotations

from typing import Any

import yaml

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
        turns = [_parse_turn(t) for t in raw["turns"]]
        return Test(turns=turns)

    trigger = raw.get("trigger", "")
    expect = _parse_expect(raw.get("expect", {}))
    return Test(trigger=trigger, expect=expect)


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
    if "name" not in raw:
        raise ValueError("function_call entry must have a 'name' field")
    return FunctionCallExpect(
        name=raw["name"],
        params=raw.get("params"),
        result=raw.get("result"),
    )
