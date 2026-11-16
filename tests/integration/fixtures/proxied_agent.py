"""An agent with a model client of its own, standard library only (HB-30).

It listens on 127.0.0.1:<argv[1]>. For each message it calls the model at
OPENAI_BASE_URL, as the OpenAI SDKs do, then writes a `turn` record with the usage it
believes it spent (PROXIED_TURN_USAGE, JSON) and answers with the model's text.

PROXIED_STREAM=1 streams the call; PROXIED_LIST=1 lists the models first; PROXIED_SYSTEM
is a system prompt, which no output of highbar may show. A 429 is retried once, as the
OpenAI and Anthropic SDKs retry it.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TRACES = os.environ.get("HIGHBAR_TRACES_URL")
BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
KEY = os.environ.get("OPENAI_API_KEY", "")
# No proxy from the environment: the agent goes where OPENAI_BASE_URL says.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def write(record: dict, test: str | None = None) -> None:
    if not TRACES:
        return
    line = {"v": 1, "ts": time.time(), **record}
    if test:
        line["context"] = {"test": test}
    fd = os.open(TRACES, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(line) + "\n").encode())
    finally:
        os.close(fd)


def call_model(message: str) -> str:
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    if os.environ.get("PROXIED_LIST"):
        listing = urllib.request.Request(f"{BASE}/models", headers=headers)
        with OPENER.open(listing, timeout=10) as reply:
            reply.read()
    stream = bool(os.environ.get("PROXIED_STREAM"))
    body = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": os.environ.get("PROXIED_SYSTEM", "")},
            {"role": "user", "content": message},
        ],
    }
    if stream:
        body["stream"] = True
    request = urllib.request.Request(
        f"{BASE}/chat/completions", data=json.dumps(body).encode(), headers=headers
    )
    try:
        with OPENER.open(request, timeout=10) as reply:
            data = reply.read().decode()
    except urllib.error.HTTPError as error:
        if error.code != 429:
            raise
        with OPENER.open(request, timeout=10) as reply:
            data = reply.read().decode()
    if not stream:
        return json.loads(data)["choices"][0]["message"]["content"]
    text = ""
    for line in data.splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            chunk = json.loads(line[6:])
            # As a hand-written loop does: it breaks on a chunk without choices.
            text += chunk["choices"][0]["delta"].get("content") or ""
    return text


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        message = json.loads(self.rfile.read(length) or b"{}").get("message", "")
        test = self.headers.get("X-Highbar-Test")
        text = call_model(message)
        usage = json.loads(os.environ.get("PROXIED_TURN_USAGE", "null"))
        write({"type": "turn", "usage": usage}, test)
        body = json.dumps({"response": text}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    write({"type": "hello", "sdk": "proxied-agent/1", "pid": os.getpid(), "tools": []})
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
