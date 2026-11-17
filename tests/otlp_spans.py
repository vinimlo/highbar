"""OTLP JSON lines as the OpenTelemetry Collector's `file` exporter writes them (HB-48).

Standard library only: tests build the spans an instrumented agent exports without an
opentelemetry dependency. Each line is one ``ExportTraceServiceRequest``.
"""

from __future__ import annotations

import json
import secrets
from typing import Any


def attr(key: str, value: Any) -> dict[str, Any]:
    """One OTLP attribute: ``{key, value: AnyValue}``. Ints go out as strings, as OTLP does."""
    if isinstance(value, bool):
        any_value: dict[str, Any] = {"boolValue": value}
    elif isinstance(value, int):
        any_value = {"intValue": str(value)}
    elif isinstance(value, float):
        any_value = {"doubleValue": value}
    else:
        any_value = {"stringValue": str(value)}
    return {"key": key, "value": any_value}


def span(
    trace_id: str,
    name: str,
    attributes: dict[str, Any],
    *,
    start: float = 1.0,
    end: float = 1.1,
    status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": secrets.token_hex(8),
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(int(start * 1e9)),
        "endTimeUnixNano": str(int(end * 1e9)),
        "attributes": [attr(key, value) for key, value in attributes.items()],
        "status": status or {},
    }
    return record


def tool_span(
    trace_id: str,
    name: str,
    arguments: dict[str, Any] | None = None,
    result: Any = None,
    **fields: Any,
) -> dict[str, Any]:
    """A GenAI `execute_tool` span. ``arguments=None`` leaves content capture off."""
    attributes: dict[str, Any] = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name}
    if arguments is not None:
        attributes["gen_ai.tool.call.arguments"] = json.dumps(arguments)
    if result is not None:
        attributes["gen_ai.tool.call.result"] = json.dumps(result)
    return span(trace_id, f"execute_tool {name}", attributes, **fields)


def openinference_tool(
    trace_id: str, name: str, arguments: dict[str, Any], result: Any = None, **fields: Any
) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "openinference.span.kind": "TOOL",
        "tool.name": name,
        "input.value": json.dumps(arguments),
    }
    if result is not None:
        attributes["output.value"] = json.dumps(result)
    return span(trace_id, name, attributes, **fields)


def agent_span(
    trace_id: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    model: str | None = None,
    response: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """A GenAI `invoke_agent` span: the turn."""
    attributes: dict[str, Any] = {"gen_ai.operation.name": "invoke_agent"}
    if input_tokens is not None:
        attributes["gen_ai.usage.input_tokens"] = input_tokens
    if output_tokens is not None:
        attributes["gen_ai.usage.output_tokens"] = output_tokens
    if model is not None:
        attributes["gen_ai.request.model"] = model
    if response is not None:
        message = {"role": "assistant", "parts": [{"type": "text", "content": response}]}
        attributes["gen_ai.output.messages"] = json.dumps([message])
    return span(trace_id, "invoke_agent", attributes, **fields)


def line(*spans: dict[str, Any]) -> str:
    """One ExportTraceServiceRequest holding ``spans``, as one line."""
    request = {
        "resourceSpans": [
            {
                "resource": {"attributes": [attr("service.name", "agent")]},
                "scopeSpans": [{"scope": {"name": "test"}, "spans": list(spans)}],
            }
        ]
    }
    return json.dumps(request) + "\n"
