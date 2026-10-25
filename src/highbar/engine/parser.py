"""YAML spec parser — converts .highbar.yaml files to domain models.

Every node is checked against the schema (``engine/schema.py``) with its path in the
spec, so an error says where it is: ``suites[0].tests[2].expect: unknown key ...``.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Any
from urllib.parse import urlsplit

import yaml

from highbar.assertions._match import validate_expected
from highbar.domain.models import (
    AgentConfig,
    CommandHook,
    Expect,
    FunctionCallExpect,
    Hook,
    PythonHook,
    RequestConfig,
    ResponseExpect,
    SessionConfig,
    Spec,
    Suite,
    Test,
    Turn,
)
from highbar.engine import placeholders
from highbar.engine.placeholders import check_placeholders, uses_placeholder
from highbar.engine.schema import SpecError, check_keys, require_type, user_keys


def parse_spec(path: str) -> Spec:
    """Parse a .highbar.yaml file into a Spec model.

    Raises ``SpecError`` whose message starts with ``path``, for any problem: missing
    file, invalid YAML, unknown key, wrong type, or a test that checks nothing.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.load(f, Loader=_StrictLoader)  # noqa: S506 — a SafeLoader subclass
    except FileNotFoundError:
        raise SpecError(f"{path}: no such file") from None
    except UnicodeDecodeError as e:
        raise SpecError(f"{path}: cannot read: not UTF-8 text (byte {e.start})") from None
    except OSError as e:
        raise SpecError(f"{path}: cannot read: {e.strerror or e}") from None
    except yaml.YAMLError as e:
        raise SpecError(f"{path}: invalid YAML: {_one_line(str(e))}") from None
    except SpecError as e:
        raise SpecError(f"{path}: {e}") from None

    try:
        return _parse_spec(raw)
    except SpecError as e:
        raise SpecError(f"{path}: {e}") from None


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects a key written twice in the same mapping.

    YAML keeps the last of two equal keys, so a second `expect:` silently replaced
    the first and its checks never ran. Keys brought in by a `<<` merge can still be
    overridden: that is what merging is for.
    """


def _mapping_without_duplicates(loader: _StrictLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=True)
        if key in seen:
            line = key_node.start_mark.line + 1
            raise SpecError(f"line {line}: duplicate key '{key}' — YAML would keep only the last")
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_without_duplicates
)


def _one_line(text: str) -> str:
    return " ".join(text.split())


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

    if not agent.managed and agent.traces is None:
        for i, suite in enumerate(suites):
            for j, test in enumerate(suite.tests):
                if test.requires_observation:
                    raise SpecError(
                        "agent: attach mode (no 'start') needs 'traces', the file the agent "
                        f"writes: suites[{i}].tests[{j}] checks function_calls"
                    )

    return Spec(agent=agent, suites=suites)


def _parse_agent(raw: Any) -> AgentConfig:
    where = "agent"
    require_type(raw, dict, where, "must be a mapping")
    check_keys(raw, "agent", where)
    raw = _expand_env(raw, where)

    if "endpoint" not in raw:
        raise SpecError(f"{where}: missing 'endpoint' (the URL highbar sends each trigger to)")
    endpoint = _required_text(raw, "endpoint")
    _check_url(endpoint, "endpoint")
    start = _required_text(raw, "start") if "start" in raw else None
    managed = start is not None
    if raw.get("health") is not None:
        require_type(raw["health"], str, where, "health must be a string (a URL)")
        _check_url(raw["health"], "health")

    if not managed and "startup_timeout" in raw:
        raise SpecError(
            f"{where}: startup_timeout only applies with 'start' "
            "(in attach mode highbar does not start the agent)"
        )
    traces = None
    if "traces" in raw:
        if managed:
            raise SpecError(
                f"{where}: 'traces' is for attach mode; with 'start', highbar creates the "
                "trace file and passes it to the agent in HIGHBAR_TRACES_URL"
            )
        traces = _required_text(raw, "traces")

    session = _parse_session(raw["session"]) if "session" in raw else None
    has_session = session is not None

    return AgentConfig(
        endpoint=endpoint,
        start=start,
        health=raw.get("health"),
        startup_timeout=_milliseconds(raw, "startup_timeout", 15000, minimum=1),
        settle_time=_milliseconds(raw, "settle_time", 500, minimum=0),
        timeout=_milliseconds(raw, "timeout", 60000, minimum=1),
        request=_parse_request(raw.get("request"), has_session),
        response=_parse_response_path(raw["response"]) if "response" in raw else None,
        traces=traces,
        session=session,
        correlate=(
            _parse_correlate(raw["correlate"], has_session)
            if "correlate" in raw
            else {"test": "{{test.id}}"}
        ),
    )


_ENV_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any, where: str) -> Any:
    """Replace ``${NAME}`` with the environment variable, once (no re-expansion).

    A missing variable is an error that names it: an empty token would send
    ``Authorization: Bearer `` and read as the agent refusing a correct request.
    """
    if isinstance(value, dict):
        return {key: _expand_env(item, f"{where}.{key}") for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item, f"{where}[{index}]") for index, item in enumerate(value)]
    if not isinstance(value, str):
        return value

    def variable(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise SpecError(f"{where}: environment variable {name} is not set")
        return os.environ[name]

    return _ENV_VAR.sub(variable, value)


def _required_text(raw: dict[str, Any], key: str) -> str:
    value = raw[key]
    require_type(value, str, "agent", f"{key} must be a string")
    if not value.strip():
        raise SpecError(f"agent: {key} cannot be empty")
    return value


def _parse_request(raw: Any, has_session: bool) -> RequestConfig:
    if raw is None:
        return RequestConfig()
    where = "agent.request"
    require_type(raw, dict, where, "must be a mapping")
    check_keys(raw, "request", where)

    method = raw.get("method", "POST")
    require_type(method, str, where, "method must be a string")
    if method.upper() not in ("POST", "PUT", "PATCH"):
        raise SpecError(f"{where}: method must be POST, PUT or PATCH, got {method!r}")

    headers = raw.get("headers", {})
    require_type(headers, dict, where, "headers must be a mapping")
    for name, value in headers.items():
        if not isinstance(value, str):
            raise SpecError(
                f"{where}.headers: {name} must be a string (quote it), got {type(value).__name__}"
            )

    body = raw.get("body", RequestConfig().body)
    # A misspelled placeholder is reported as such, before "never uses {{message}}".
    check_placeholders(
        headers, f"{where}.headers", allowed=placeholders.ALL, has_session=has_session
    )
    check_placeholders(body, f"{where}.body", allowed=placeholders.ALL, has_session=has_session)
    if not uses_placeholder(body, "message"):
        raise SpecError(
            f"{where}.body: never uses {{{{message}}}}, so the trigger would not be sent"
        )
    return RequestConfig(method=method.upper(), headers=dict(headers), body=body)


_RESPONSE_PATH = re.compile(r"[^.\s]+(?:\.[^.\s]+)*")


def _parse_response_path(raw: Any) -> str:
    require_type(raw, str, "agent", "response must be a string (a path like data.reply)")
    if not _RESPONSE_PATH.fullmatch(raw):
        raise SpecError(
            f"agent: response must be a dotted path to the reply text, like data.reply "
            f"or choices.0.message.content, got {raw!r}"
        )
    return raw


def _parse_session(raw: Any) -> SessionConfig:
    where = "agent.session"
    require_type(raw, dict, where, "must be a mapping with setup and/or teardown")
    check_keys(raw, "session", where)
    if "setup" not in raw and "teardown" not in raw:
        raise SpecError(f"{where}: needs setup or teardown")
    return SessionConfig(
        setup=_parse_hook(raw["setup"], f"{where}.setup", placeholders.SETUP)
        if "setup" in raw
        else None,
        teardown=_parse_hook(raw["teardown"], f"{where}.teardown", placeholders.TEARDOWN)
        if "teardown" in raw
        else None,
    )


def _parse_hook(raw: Any, where: str, allowed: frozenset[str]) -> Hook:
    """A command (text, split like a shell would, run without one) or ``{python: ...}``."""
    if isinstance(raw, str):
        try:
            argv = shlex.split(raw)
        except ValueError as e:
            raise SpecError(f"{where}: not a valid command line: {e}") from None
        if not argv:
            raise SpecError(f"{where}: cannot be empty")
        check_placeholders(argv, where, allowed=allowed, has_session=True)
        return CommandHook(argv=argv)

    require_type(raw, dict, where, "must be a command or {python: 'module:function'}")
    check_keys(raw, "python_hook", where)
    target = raw.get("python")
    require_type(target, str, where, "python must be 'module:function'")
    module, colon, function = target.partition(":")
    if not colon or not module.strip() or not function.strip() or ":" in function:
        raise SpecError(f"{where}: python must be 'module:function', got {target!r}")
    return PythonHook(target=target)


def _parse_correlate(raw: Any, has_session: bool) -> dict[str, Any]:
    where = "agent.correlate"
    if not isinstance(raw, dict) or not raw:
        raise SpecError(
            "agent: correlate must be a non-empty mapping of context key to value "
            '(e.g. {conversation_id: "{{session.conversation_id}}"})'
        )
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise SpecError(f"{where}.{key}: must be text, a number or a placeholder")
    check_placeholders(raw, where, allowed=placeholders.ALL, has_session=has_session)
    return dict(raw)


def _check_url(url: str, key: str) -> None:
    """An http(s) URL with a host. Without a scheme, `localhost:8000/chat` reads as port 80."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SpecError(f"agent: {key} must be an http:// or https:// URL, got {url!r}")
    try:
        parts.port  # noqa: B018 — parsing the port is the check
    except ValueError:
        raise SpecError(f"agent: {key} has an invalid port: {url!r}") from None


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
        if key not in raw:
            continue
        value = raw[key]
        require_type(value, str, response_where, f"{key} must be a string")
        if not value:
            # "" is in every answer (and matches every answer): a check that cannot fail.
            raise SpecError(f"{response_where}: {key} cannot be empty")
        if _has_template(value):
            raise SpecError(
                f"{response_where}.{key}: templates are only resolved in triggers; "
                "here '{{...}}' would be compared as literal text"
            )

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
        if value is not None:
            # Matched key by key against the call's arguments or return value.
            require_type(value, dict, where, f"{field} must be a mapping of expected values")
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
