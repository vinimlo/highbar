"""OpenTelemetry spans read as trace records (HB-48): OTLP JSON lines.

Each line is one ``ExportTraceServiceRequest`` in OTLP/JSON, as the OpenTelemetry
Collector's ``file`` exporter writes it: ``resourceSpans[].scopeSpans[].spans[]``.

The mapping is versioned here, with the conventions it was written against:

- OpenTelemetry's GenAI semantic conventions, as published in Development status (they
  may still change): a span with ``gen_ai.operation.name = execute_tool`` is a tool
  call (``gen_ai.tool.name``, ``gen_ai.tool.call.arguments``,
  ``gen_ai.tool.call.result``); ``invoke_agent`` is a turn (``gen_ai.output.messages``,
  ``gen_ai.usage.input_tokens`` and ``output_tokens``, ``gen_ai.response.model`` or
  ``gen_ai.request.model``, ``gen_ai.provider.name`` or ``gen_ai.system``).
- OpenInference span kinds: ``openinference.span.kind = TOOL`` is a tool call
  (``tool.name``, ``input.value``, ``output.value``); ``AGENT`` is a turn
  (``output.value``, ``llm.token_count.prompt`` and ``completion``, ``llm.model_name``,
  ``llm.provider``).

A failed call is a span with status ERROR or an ``error.type``. Spans of other kinds
(``chat``/``LLM``, ``RETRIEVER``...) are not read in v0.

A record's context is ``{"test": <trace id>}``: highbar sends each test execution's id
as the trace id of its ``traceparent``, so the default ``agent.correlate`` matches a
span to its test. The runner reads a span of a trace it did not send as untagged.

Arguments and results are opt-in in the GenAI conventions. A tool span without its
arguments is read with ``has_args`` False, so a ``params`` check on it is unverified,
never a pass on an empty ``{}``.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

from highbar._narrow import as_dict, as_list
from highbar.domain.models import Trace, TurnRecord, Usage, Window

OTLP_MAPPING_VERSION = 0
OTLP_CONVENTIONS = (
    "OpenTelemetry GenAI semantic conventions (Development status), OpenInference span kinds"
)

# OTLP/JSON's STATUS_CODE_ERROR, as a number or by its name.
_STATUS_ERROR = (2, "2", "STATUS_CODE_ERROR")
_TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_NO_TRACE = "0" * 32


class OtlpLineError(ValueError):
    """A line of OTLP that cannot be read: the collector names its line."""


def add_otlp(window: Window, request: dict[str, Any]) -> None:
    """Add the tool calls and turns of one ExportTraceServiceRequest to ``window``.

    Raises ``OtlpLineError`` for the whole line, before adding anything: a span that
    cannot be read could be a tool call, and half a line would hide it.
    """
    tools: list[Trace] = []
    turns: list[TurnRecord] = []
    for raw_span in _spans(request):
        span = as_dict(raw_span)
        if span is None:
            raise OtlpLineError("a span must be a JSON object")
        attrs = _attributes(span.get("attributes"))
        kind = _kind(attrs)
        if kind == "tool":
            tools.append(_tool(span, attrs))
        elif kind == "turn":
            turns.append(_turn(span, attrs))
    window.tools.extend(tools)
    window.turns.extend(turns)


def _spans(request: dict[str, Any]) -> list[Any]:
    """Every span of the request. A line of logs or metrics has none, and is ignored."""
    if "resourceSpans" not in request:
        return []
    resources = as_list(request["resourceSpans"])
    if resources is None:
        raise OtlpLineError("resourceSpans must be a list")
    spans: list[Any] = []
    for raw_resource in resources:
        resource = as_dict(raw_resource)
        scopes = as_list(resource.get("scopeSpans", [])) if resource is not None else None
        if scopes is None:
            raise OtlpLineError("each of resourceSpans must be an object with a scopeSpans list")
        for raw_scope in scopes:
            scope = as_dict(raw_scope)
            found = as_list(scope.get("spans", [])) if scope is not None else None
            if found is None:
                raise OtlpLineError("each of scopeSpans must be an object with a spans list")
            spans.extend(found)
    return spans


def _kind(attrs: dict[str, Any]) -> str | None:
    operation = attrs.get("gen_ai.operation.name")
    if operation == "execute_tool":
        return "tool"
    if operation == "invoke_agent":
        return "turn"
    span_kind = attrs.get("openinference.span.kind")
    if isinstance(span_kind, str):
        span_kind = span_kind.upper()
    if span_kind == "TOOL":
        return "tool"
    if span_kind == "AGENT":
        return "turn"
    return None


def _tool(span: dict[str, Any], attrs: dict[str, Any]) -> Trace:
    genai = attrs.get("gen_ai.operation.name") == "execute_tool"
    name = attrs.get("gen_ai.tool.name") if genai else attrs.get("tool.name")
    if not isinstance(name, str) or not name:
        # The GenAI convention names the span `execute_tool {gen_ai.tool.name}`.
        span_name = span.get("name")
        prefix = "execute_tool "
        if genai and isinstance(span_name, str) and span_name.startswith(prefix):
            name = span_name[len(prefix) :].strip()
        elif not genai and isinstance(span_name, str):
            name = span_name.strip()
    if not isinstance(name, str) or not name:
        which = (
            "execute_tool span without gen_ai.tool.name" if genai else "TOOL span without tool.name"
        )
        raise OtlpLineError(which)
    args_key = "gen_ai.tool.call.arguments" if genai else "input.value"
    result_key = "gen_ai.tool.call.result" if genai else "output.value"
    has_args = attrs.get(args_key) is not None
    args: dict[str, Any] = {}
    if has_args:
        parsed = _json_value(attrs[args_key])
        found = as_dict(parsed)
        if found is None:
            raise OtlpLineError(
                f"{args_key} of '{name}' must be a JSON object, got {type(parsed).__name__}"
            )
        args = found
    has_result = attrs.get(result_key) is not None
    start, end = _seconds(span, "startTimeUnixNano"), _seconds(span, "endTimeUnixNano")
    return Trace(
        fn=name,
        args=args,
        result=_json_value(attrs[result_key]) if has_result else None,
        has_result=has_result,
        has_args=has_args,
        started_at=start if start is not None else 0.0,
        ended_at=end if end is not None else (start or 0.0),
        error=_error(span, attrs),
        context={"test": _trace_id(span)},
    )


def _turn(span: dict[str, Any], attrs: dict[str, Any]) -> TurnRecord:
    genai = attrs.get("gen_ai.operation.name") == "invoke_agent"
    if genai:
        usage = Usage(
            model=_label(attrs.get("gen_ai.response.model"))
            or _label(attrs.get("gen_ai.request.model")),
            provider=_label(attrs.get("gen_ai.provider.name"))
            or _label(attrs.get("gen_ai.system")),
            input_tokens=_count(attrs.get("gen_ai.usage.input_tokens")),
            output_tokens=_count(attrs.get("gen_ai.usage.output_tokens")),
        )
        response = _output_text(attrs.get("gen_ai.output.messages"))
    else:
        usage = Usage(
            model=_label(attrs.get("llm.model_name")),
            provider=_label(attrs.get("llm.provider")),
            input_tokens=_count(attrs.get("llm.token_count.prompt")),
            output_tokens=_count(attrs.get("llm.token_count.completion")),
        )
        output = attrs.get("output.value")
        response = output if isinstance(output, str) else None
    start, end = _seconds(span, "startTimeUnixNano"), _seconds(span, "endTimeUnixNano")
    return TurnRecord(
        ts=end if end is not None else (start or 0.0),
        context={"test": _trace_id(span)},
        response=response,
        usage=None if usage == Usage(model=usage.model, provider=usage.provider) else usage,
    )


def _output_text(raw: Any) -> str | None:
    """The text of the last assistant message of ``gen_ai.output.messages``, if any."""
    messages = as_list(_json_value(raw)) if raw is not None else None
    if not messages:
        return None
    for message in reversed(messages):
        fields = as_dict(message)
        if fields is None or fields.get("role", "assistant") != "assistant":
            continue
        parts = as_list(fields.get("parts")) or []
        texts = [
            str(part["content"])
            for part in (as_dict(p) for p in parts)
            if part is not None and part.get("type") == "text" and part.get("content") is not None
        ]
        if texts:
            return "".join(texts)
    return None


def _error(span: dict[str, Any], attrs: dict[str, Any]) -> str | None:
    status = as_dict(span.get("status")) or {}
    failed = status.get("code") in _STATUS_ERROR
    error_type = _label(attrs.get("error.type"))
    if not failed and error_type is None:
        return None
    message = _label(status.get("message"))
    return message or error_type or "ERROR"


def _trace_id(span: dict[str, Any]) -> str:
    """The span's trace id as 32 lower-case hex digits.

    OTLP/JSON writes it in hex; a protobuf JSON mapping writes bytes as base64, which
    is read too. Without one the span belongs to no test, and could hide a call.
    """
    raw = span.get("traceId")
    if isinstance(raw, str) and _TRACE_ID.match(raw):
        trace_id = raw.lower()
    elif isinstance(raw, str):
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            decoded = b""
        trace_id = decoded.hex() if len(decoded) == 16 else ""
    else:
        trace_id = ""
    if not trace_id or trace_id == _NO_TRACE:
        raise OtlpLineError(f"span without a valid traceId: '{span.get('name', '')}'")
    return trace_id


def _attributes(raw: Any) -> dict[str, Any]:
    """OTLP's ``[{key, value: AnyValue}]`` as a plain mapping."""
    items = as_list(raw)
    if raw is not None and items is None:
        raise OtlpLineError("attributes must be a list")
    attrs: dict[str, Any] = {}
    for item in items or []:
        pair = as_dict(item)
        if pair is None or not isinstance(pair.get("key"), str):
            raise OtlpLineError("each attribute must be an object with a 'key'")
        attrs[pair["key"]] = _any_value(pair.get("value"))
    return attrs


def _any_value(raw: Any) -> Any:
    value = as_dict(raw)
    if value is None:
        return None
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return value["boolValue"]
    if "intValue" in value:
        number = value["intValue"]
        # OTLP/JSON writes 64-bit integers as strings.
        try:
            return int(number)
        except (TypeError, ValueError):
            return None
    if "doubleValue" in value:
        return value["doubleValue"]
    if "arrayValue" in value:
        array = as_dict(value["arrayValue"]) or {}
        return [_any_value(item) for item in as_list(array.get("values")) or []]
    if "kvlistValue" in value:
        kvlist = as_dict(value["kvlistValue"]) or {}
        return _attributes(kvlist.get("values"))
    if "bytesValue" in value:
        return value["bytesValue"]
    return None


def _json_value(raw: Any) -> Any:
    """An attribute that holds JSON as text, parsed; text that is not JSON stays text."""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _seconds(span: dict[str, Any], key: str) -> float | None:
    raw = span.get(key)
    if isinstance(raw, bool):
        return None
    try:
        return int(raw) / 1e9 if isinstance(raw, (str, int)) else None
    except ValueError:
        return None


def _label(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _count(value: Any) -> int | None:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
