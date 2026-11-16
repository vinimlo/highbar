"""A stand-in agent that exports its spans as OTLP JSON lines (HB-48). Stdlib only.

It writes what an agent instrumented with OpenTelemetry's GenAI conventions exports
through the Collector's `file` exporter: one ExportTraceServiceRequest per line, an
`execute_tool` span per tool call and an `invoke_agent` span for the turn. Like an
instrumented server, it continues the trace of the incoming W3C `traceparent`.

A message with "refund" calls `refund`; any other calls `get_order` with the order id
it names. Switches:
  --no-content          leave out the tool's arguments and result (capture off)
  --ignore-traceparent  start a new trace per request, as an uninstrumented server does
  --seen FILE           append each traceparent received to FILE
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _nanos(seconds: float) -> str:
    return str(int(seconds * 1e9))


def _span(
    trace_id: str, parent: str, name: str, attrs: dict[str, Any], start: float, end: float
) -> dict[str, Any]:
    return {
        "traceId": trace_id,
        "spanId": secrets.token_hex(8),
        "parentSpanId": parent,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": _nanos(start),
        "endTimeUnixNano": _nanos(end),
        "attributes": [_attr(key, value) for key, value in attrs.items()],
        "status": {},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--traces", required=True)
    parser.add_argument("--no-content", action="store_true")
    parser.add_argument("--ignore-traceparent", action="store_true")
    parser.add_argument("--seen")
    options = parser.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            message = str(json.loads(self.rfile.read(length) or b"{}").get("message", ""))
            header = self.headers.get("traceparent", "")
            if options.seen:
                with open(options.seen, "a", encoding="utf-8") as f:
                    f.write(header + "\n")
            match = TRACEPARENT.match(header)
            if match and not options.ignore_traceparent:
                trace_id, parent = match.group(1), match.group(2)
            else:
                trace_id, parent = secrets.token_hex(16), ""

            start = time.time()
            agent_id = secrets.token_hex(8)
            if "refund" in message:
                tool, args, result = "refund", {"order": "A1"}, {"refunded": True}
            else:
                order = re.search(r"[A-Z]\d+", message)
                tool = "get_order"
                args = {"id": order.group(0) if order else ""}
                result = {"status": "shipped"}
            attrs: dict[str, Any] = {
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": tool,
            }
            if not options.no_content:
                attrs["gen_ai.tool.call.arguments"] = json.dumps(args)
                attrs["gen_ai.tool.call.result"] = json.dumps(result)
            tool_span = _span(trace_id, agent_id, f"execute_tool {tool}", attrs, start, time.time())
            reply = f"Order {args.get('id') or args.get('order')}: done."
            turn = _span(
                trace_id,
                parent,
                "invoke_agent support",
                {
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.request.model": "gpt-4o-mini",
                    "gen_ai.usage.input_tokens": 120,
                    "gen_ai.usage.output_tokens": 12,
                    "gen_ai.output.messages": json.dumps(
                        [{"role": "assistant", "parts": [{"type": "text", "content": reply}]}]
                    ),
                },
                start,
                time.time(),
            )
            turn["spanId"] = agent_id
            export = {
                "resourceSpans": [
                    {
                        "resource": {"attributes": [_attr("service.name", "support-agent")]},
                        "scopeSpans": [
                            {"scope": {"name": "otlp-agent"}, "spans": [tool_span, turn]}
                        ],
                    }
                ]
            }
            with open(options.traces, "a", encoding="utf-8") as f:
                f.write(json.dumps(export) + "\n")

            body = json.dumps({"response": reply}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    ThreadingHTTPServer(("127.0.0.1", options.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
