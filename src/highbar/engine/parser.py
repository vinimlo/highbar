"""YAML spec parser — converts .highbar.yaml files to domain models.

Every node is checked against the schema (``engine/schema.py``) with its path in the
spec, so an error says where it is: ``suites[0].tests[2].expect: unknown key ...``.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from highbar.assertions._match import validate_expected
from highbar.domain.models import (
    AgentConfig,
    Expect,
    FunctionCallExpect,
    ResponseExpect,
    Spec,
    Suite,
    Test,
    Turn,
)
from highbar.engine.schema import SpecError, check_keys, require_type, user_keys


def parse_spec(path: str) -> Spec:
    """Parse a .highbar.yaml file into a Spec model.

    Raises ``SpecError`` whose message starts with ``path``, for any problem: missing
    file, invalid YAML, unknown key, wrong type, or a test that checks nothing.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raise SpecError(f"{path}: no such file") from None
    except yaml.YAMLError as e:
        raise SpecError(f"{path}: invalid YAML: {e}") from None

    try:
        return _parse_spec(raw)
    except SpecError as e:
        raise SpecError(f"{path}: {e}") from None


def _parse_spec(raw: Any) -> Spec:
    require_type(raw, dict, "spec", "must be a mapping with 'agent' and 'suites'")
    check_keys(raw, "spec", "spec")
    if "agent" not in raw:
        raise SpecError("spec: missing 'agent' (how to start and reach the agent)")

    agent = _parse_agent(raw["agent"])

    suites_raw = raw.get("suites")
    if not suites_raw:
        raise SpecError("spec: has no tests (add a suite under 'suites')")
    require_type(suites_raw, list, "suites", "must be a list of suites")
    suites = [_parse_suite(s, f"suites[{i}]") for i, s in enumerate(suites_raw)]

    return Spec(agent=agent, suites=suites)


def _parse_agent(raw: Any) -> AgentConfig:
    where = "agent"
    require_type(raw, dict, where, "must be a mapping")
    check_keys(raw, "agent", where)
    for key in ("start", "endpoint"):
        if key not in raw:
            raise SpecError(f"{where}: missing '{key}'")
        require_type(raw[key], str, where, f"{key} must be a string")
    if raw.get("health") is not None:
        require_type(raw["health"], str, where, "health must be a string (a URL)")

    return AgentConfig(
        start=raw["start"],
        endpoint=raw["endpoint"],
        health=raw.get("health"),
        startup_timeout=_milliseconds(raw, "startup_timeout", 15000, minimum=1),
        settle_time=_milliseconds(raw, "settle_time", 500, minimum=0),
    )


def _milliseconds(raw: dict[str, Any], key: str, default: int, minimum: int) -> int:
    value = raw.get(key, default)
    require_type(value, int, "agent", f"{key} must be an integer (milliseconds)")
    if value < minimum:
        qualifier = "a positive" if minimum > 0 else "a non-negative"
        raise SpecError(f"agent: {key} must be {qualifier} integer (milliseconds), got {value}")
    return value


def _parse_suite(raw: Any, where: str) -> Suite:
    require_type(raw, dict, where, "a suite must be a mapping")
    check_keys(raw, "suite", where)
    name = _optional_name(raw, where) or "Unnamed Suite"

    tests_raw = raw.get("tests")
    if not tests_raw:
        raise SpecError(f"{where}: has no tests")
    require_type(tests_raw, list, where, "tests must be a list")
    tests = [_parse_test(t, f"{where}.tests[{j}]") for j, t in enumerate(tests_raw)]
    return Suite(name=name, tests=tests)


def _optional_name(raw: dict[str, Any], where: str) -> str | None:
    name = raw.get("name")
    if name is not None:
        require_type(name, str, where, "name must be a string")
    return name


def _parse_test(raw: Any, where: str) -> Test:
    require_type(raw, dict, where, "a test must be a mapping")
    check_keys(raw, "test", where)
    name = _optional_name(raw, where)

    if "turns" in raw:
        if "trigger" in raw:
            raise SpecError(f"{where}: a test has either 'trigger' or 'turns', not both")
        if "expect" in raw:
            raise SpecError(f"{where}: with 'turns', each turn has its own 'expect'")
        if raw.get("variations"):
            # Paraphrasing turn 1 would leave the later turns answering a conversation
            # that no longer happened. Multi-turn variations need a design of their own.
            raise SpecError(f"{where}: 'variations' is not supported on multi-turn tests")
        return Test(turns=_parse_turns(raw["turns"], where), name=name)

    trigger = _trigger(raw, where)
    if "expect" not in raw:
        raise SpecError(
            f"{where}: asserts nothing — add an 'expect' "
            "(function_calls: [] checks that no tool is called)"
        )
    expect = _parse_expect(raw["expect"], f"{where}.expect")
    variations = _parse_variations(raw.get("variations"), where)
    return Test(trigger=trigger, expect=expect, variations=variations, name=name)


def _trigger(raw: dict[str, Any], where: str) -> str:
    if "trigger" not in raw:
        raise SpecError(f"{where}: missing 'trigger' (the message sent to the agent)")
    trigger = raw["trigger"]
    require_type(trigger, str, where, "trigger must be a string")
    if not trigger.strip():
        raise SpecError(f"{where}: trigger cannot be empty")
    return trigger


def _parse_turns(raw: Any, where: str) -> list[Turn]:
    require_type(raw, list, where, "turns must be a list")
    if not raw:
        raise SpecError(f"{where}: turns cannot be empty")

    turns = [_parse_turn(t, f"{where}.turns[{k}]") for k, t in enumerate(raw)]
    if not any(_asserts_something(turn.expect) for turn in turns):
        raise SpecError(f"{where}: asserts nothing — no turn has an 'expect'")
    return turns


def _parse_turn(raw: Any, where: str) -> Turn:
    require_type(raw, dict, where, "a turn must be a mapping")
    check_keys(raw, "turn", where)
    trigger = _trigger(raw, where)
    # A turn without `expect` only sets the scene for the next ones (a greeting, a fact
    # the agent must remember). Writing `expect: {}` is different: it says "check
    # this" and then checks nothing, and is rejected in _parse_expect.
    expect = _parse_expect(raw["expect"], f"{where}.expect") if "expect" in raw else Expect()
    return Turn(trigger=trigger, expect=expect, name=_optional_name(raw, where))


def _asserts_something(expect: Expect) -> bool:
    return expect.function_calls is not None or expect.response is not None


def _parse_variations(raw: Any, where: str) -> int:
    if raw is None:
        return 0
    require_type(raw, int, where, "variations must be a non-negative integer")
    if raw < 0:
        raise SpecError(f"{where}: variations must be a non-negative integer, got {raw}")
    return raw


def _parse_expect(raw: Any, where: str) -> Expect:
    require_type(raw, dict, where, "expect must be a mapping")
    check_keys(raw, "expect", where)
    if not user_keys(raw):
        raise SpecError(
            f"{where}: asserts nothing — use function_calls, response, or "
            "function_calls: [] to check that no tool is called"
        )

    function_calls = None
    if "function_calls" in raw:
        calls = raw["function_calls"]
        require_type(calls, list, where, "function_calls must be a list")
        function_calls = [
            _parse_function_call(fc, f"{where}.function_calls[{n}]") for n, fc in enumerate(calls)
        ]

    response = None
    if "response" in raw:
        response = _parse_response(raw["response"], where)

    return Expect(function_calls=function_calls, response=response)


def _parse_response(raw: Any, where: str) -> ResponseExpect:
    if raw is None or (isinstance(raw, dict) and not user_keys(raw)):
        raise SpecError(f"{where}: response has no matcher (use contains, not_contains or matches)")
    require_type(raw, dict, where, "response must be a mapping")
    response_where = f"{where}.response"
    check_keys(raw, "response", response_where)
    for key in ("contains", "not_contains", "matches"):
        if key in raw:
            require_type(raw[key], str, response_where, f"{key} must be a string")

    if "matches" in raw:
        try:
            re.compile(raw["matches"])
        except re.error as e:
            raise SpecError(f"{response_where}.matches: invalid regex: {e}") from None

    return ResponseExpect(
        contains=raw.get("contains"),
        not_contains=raw.get("not_contains"),
        matches=raw.get("matches"),
    )


def _parse_function_call(raw: Any, where: str) -> FunctionCallExpect:
    require_type(raw, dict, where, "a function_calls entry must be a mapping")
    check_keys(raw, "function_call", where)
    if "name" not in raw:
        raise SpecError(f"{where}: missing 'name' (the tool expected to be called)")

    for field in ("params", "result"):
        value = raw.get(field)
        if _has_template(value):
            raise SpecError(
                f"{where}.{field}: templates are only resolved in triggers; "
                "here '{{...}}' would be compared as literal text"
            )
        # A malformed matcher must fail at `highbar validate`, not in the middle of a run.
        try:
            validate_expected(value, f"{where}.{field}")
        except ValueError as e:
            raise SpecError(str(e)) from None

    return FunctionCallExpect(
        name=_parse_call_name(raw["name"], where),
        params=raw.get("params"),
        result=raw.get("result"),
    )


def _has_template(value: Any) -> bool:
    if isinstance(value, str):
        return "{{" in value
    if isinstance(value, dict):
        return any(_has_template(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_template(v) for v in value)
    return False


def _parse_call_name(raw: Any, where: str) -> str | list[str]:
    """Accept a tool name, or a list of tools of which any one satisfies the expectation.

    Validated here rather than left to the assertion: a spec that says
    ``name: [a, 3]`` or ``name: ""`` should fail loudly at `highbar validate`, not
    silently never match at run time.
    """
    if isinstance(raw, str):
        if not raw.strip():
            raise SpecError(f"{where}: function_call 'name' cannot be empty")
        return raw

    if isinstance(raw, list):
        if not raw:
            raise SpecError(f"{where}: function_call 'name' cannot be an empty list")
        if not all(isinstance(name, str) and name.strip() for name in raw):
            msg = f"function_call 'name' list must contain only non-empty strings, got {raw!r}"
            raise SpecError(f"{where}: {msg}")
        # Listing a tool twice adds nothing, and would repeat it in failure messages.
        return list(dict.fromkeys(raw))

    raise SpecError(
        f"{where}: function_call 'name' must be a string or a list of strings, "
        f"got {type(raw).__name__}"
    )
