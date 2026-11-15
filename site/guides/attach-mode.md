# Attach mode and sessions

Without `agent.start`, highbar attaches to an agent that is already running: a staging deploy, a container, an agent that needs its real database. highbar never starts or stops it, and never touches its trace file.

```yaml agent
agent:
  endpoint: "http://localhost:8000/api/chat"
  health: "http://localhost:8000/health"
  traces: "var/traces.jsonl"
```

- Before any test, `health` must answer (or, without `health`, the endpoint's port must accept connections). Otherwise the run stops with exit code 2: `nothing answers at localhost:8000`.
- `traces` is the file the agent writes, relative to where you run highbar. A test that checks tool calls needs it, and the spec is rejected without it. A spec that only checks replies can run against any HTTP agent, traced or not.
- When a test checks tool calls or has a budget, the file must exist before the run. Otherwise the run stops with exit code 2 before sending anything, and the message shows the absolute path it looked for. highbar's SDK creates the file when `Highbar()` is created, and after the file is deleted, only with its next record. Create the file empty when it was deleted while the agent ran, or when its writer creates it only with its first record.
- Other traffic may reach an attached agent, so a test counts only the trace records tagged as its own. See [correlation](#which-records-belong-to-a-test) below.

## The request

By default highbar sends `POST {"message": "<trigger>"}`. Any other shape is configured with placeholders:

::: v-pre
| Placeholder | Value |
|---|---|
| `{{message}}` | the trigger |
| `{{test.id}}` | this execution's id, also sent in the `X-Highbar-Test` header |
| `{{test.name}}` | the test's name, or its trigger |
| `{{test.args.<path>}}` | a value of the test's own `session.args` |
| `{{session.<path>}}` | a value the session's setup returned |
:::

```yaml agent
agent:
  endpoint: "https://staging.example.com/api/v2/conversations/messages"
  traces: "/var/log/support-agent/traces.jsonl"
  request:
    method: POST
    headers:
      Authorization: "Bearer ${AGENT_TOKEN}"
      X-Tenant: "acme"
    body:
      input: {text: "{{message}}", channel: "web"}
      conversation_id: "{{session.conversation_id}}"
  response: "data.reply.text"
  timeout: 30000
  session:
    setup: "python scripts/open_conversation.py"
```

- A placeholder that is a whole value keeps its JSON type: a number stays a number. Inside a longer text it is inserted as text.
- The body must send <code v-pre>{{message}}</code> somewhere, or every test would send the same request.
- `X-Highbar-Test` is always sent, whatever the headers say.
- `${NAME}` takes the value of an environment variable when the spec is loaded, under `agent` only. A value of 6 characters or more never appears in highbar's output: test errors, warnings and spec errors show `${NAME}` instead. A shorter value, such as a port, is rarely secret and is shown as it is. A header value with a newline or surrounding spaces (a token read from a file with its line break) is a spec error that does not show the value.

## The reply

`response` is a dotted path into the JSON reply (`data.reply.text`, `choices.0.message.content`). Without it, highbar takes the first of the keys `response`, `message`, `content`, `text` and `reply`, or the body itself when it is plain text.

These are errors, never a reply checked as empty text:

- a status other than 2xx (a redirect also says where it points);
- an empty body, or a reply value that is `null`;
- no reply within `timeout` milliseconds (60000 by default; a reply that trickles in counts as a whole);
- a body over 10 MB.

## A session per test

Many agents keep state per conversation. A session hook opens a clean one before each test execution and closes it after, so tests do not see each other's history:

```yaml agent
agent:
  endpoint: "http://localhost:8000/api/chat"
  traces: "var/traces.jsonl"
  request:
    body: {message: "{{message}}", conversation_id: "{{session.conversation_id}}"}
  session:
    setup: "python scripts/open_conversation.py --label {{test.name}}"
    teardown: "python scripts/close_conversation.py {{session.conversation_id}}"
  correlate: {conversation_id: "{{session.conversation_id}}"}
```

- **A command** runs without a shell, in its own process group, and is killed if it outlives `agent.timeout`. The last non-empty line it prints must be a JSON object, which becomes <code v-pre>{{session.*}}</code>; it may log before that line.
- **A Python function** is given as `{python: "module:function"}`, imported from the directory you run highbar in, before any agent starts. `setup` receives an object with `test_id`, `test_name`, `spec_path` and `args`, and returns a mapping; `teardown` receives the same object and the session.
- **An HTTP request** is given as `{http: {url, method, headers, body}}`. highbar sends it itself, so it can run from an image of its own, away from the agent's. Placeholders work in the URL, the headers and the body. `method` is `POST` by default, and may be `GET`, `PUT`, `PATCH` or `DELETE`; `body` is sent as JSON, a text as plain text, and nothing when it is left out. In the URL, placeholders go in the path and the query, not in the scheme or the host, and each value is one percent-encoded piece: a `/`, `?`, `#` or `@` in a session value cannot send the request to another resource or host, and a value that is `.` or `..` is refused. A header carries ASCII only; an accented test name goes in the body or the URL. A setup's reply must be a JSON object, which becomes <code v-pre>{{session.*}}</code>. A reply that is not 2xx, a redirect included, is the hook's failure, and so is no reply within `agent.timeout`.
- A `headers` key in the session is added to the request's headers.

With HTTP hooks, the same session as above:

```yaml agent
agent:
  endpoint: "http://localhost:8000/api/chat"
  traces: "var/traces.jsonl"
  request:
    body: {message: "{{message}}", conversation_id: "{{session.conversation_id}}"}
  session:
    setup:
      http:
        url: "http://localhost:8000/test/conversations"
        headers: {Authorization: "Bearer ${EVALS_TOKEN}"}
        body: {label: "{{test.name}}"}
    teardown:
      http:
        url: "http://localhost:8000/test/conversations/{{session.conversation_id}}"
        method: DELETE
  correlate: {conversation_id: "{{session.conversation_id}}"}
```

- A setup that fails makes the test an `error` without sending anything. A teardown that fails is a warning; the result stands.
- Each run of a test is its own execution with its own session. The turns of a multi-turn test share one.

### A state of its own

A test can start from a state of its own (a contact whose data was already collected, a ticket already closed) without a spec per state. Under the test, `session: {args: {...}}` holds a JSON object. Its hooks receive it as `args` in Python, and as <code v-pre>{{test.args.&lt;path&gt;}}</code> in a command, an HTTP hook, the request and `correlate`:

```yaml
agent:
  endpoint: "http://localhost:8000/api/chat"
  traces: "var/traces.jsonl"
  session:
    setup: "python scripts/open_ticket.py --stage {{test.args.stage}}"
suites:
  - name: Support
    tests:
      - trigger: "Can you reopen my ticket?"
        session: {args: {stage: closed}}
        expect:
          function_calls:
            - name: reopen_ticket
      - trigger: "Is my ticket still open?"
        session: {args: {stage: open}}
        expect:
          function_calls:
            - name: get_ticket
```

Every <code v-pre>{{test.args.&lt;path&gt;}}</code> the agent uses must have a value in every test, and not `null`: the spec does not load otherwise, and the message names the test and the key. A trigger does not read them: it goes to the agent as written, so <code v-pre>{{test.args.&lt;path&gt;}}</code> in a trigger stops the spec too. Args that the agent does not use can differ from test to test. They are part of what a test measures, so they change its `definition_sha256`.

## Which records belong to a test

A test counts the trace records whose `context` matches `agent.correlate`. The default, <code v-pre>{test: "{{test.id}}"}</code>, matches the test id that `highbar.asgi.Middleware` and `hb.turn()` put in every record. When the test id cannot reach the code that records the calls, correlate on what can, like the conversation the session opened (as above).

In attach mode, a test that checks tool calls needs at least one record of its own. Without it, the test is an `error` that says what was read:

- nothing was written while the test ran;
- the records read carry no context, so they cannot be told from other traffic;
- the records carry context, but not the keys of `agent.correlate`.
