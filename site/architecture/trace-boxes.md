# Trace Boxes

A **trace box** is the atomic unit of data captured by the `@hb.tool` decorator. Each tool invocation produces exactly one trace box, written as a single line in a JSONL file.

## Format

Each trace box is a JSON object with the following fields:

```json
{
  "id": "a1b2c3d4e5f6g7h8",
  "seq": 0,
  "fn": "search_orders",
  "args": {"order_id": "12345"},
  "result": {"order_id": "12345", "status": "shipped"},
  "started_at": 1710000000.123,
  "ended_at": 1710000000.456
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique fingerprint (16-char hex) |
| `seq` | int | Auto-incrementing sequence number |
| `fn` | string | Function name |
| `args` | object | Function arguments as key-value pairs |
| `result` | any | Return value of the function |
| `started_at` | float | Start timestamp (`time.time()`) |
| `ended_at` | float | End timestamp |
| `error` | string | (Optional) Error message if the function raised an exception |

## Fingerprint

Each trace box receives a unique ID computed as:

```
sha256(json({"fn": fn, "args": args, "nonce": uuid4_hex}, sort_keys=True))[:16]
```

Properties of this fingerprint:

- **Collision-resistant**: The `uuid4` nonce ensures that even identical function calls with the same arguments produce different IDs.
- **Deterministic per-invocation**: The same invocation always produces the same fingerprint (the nonce is generated once per call).
- **Compact**: 16 hex characters (64 bits of entropy) is sufficient for test correlation.

Example: calling `search_orders(order_id="12345")` twice produces two different IDs because each call generates a fresh UUID nonce.

## Sequence Counter

The `seq` field is an auto-incrementing integer starting at 0. It provides a total ordering of trace boxes within a single test run, independent of timestamps.

The counter is managed by the `TraceWriter` class and is incremented under a `threading.Lock`, making it safe for concurrent tool calls.

## File Transport

Trace boxes are written to a JSONL (JSON Lines) file -- one JSON object per line, separated by newlines.

```
{"id":"a1b2...","seq":0,"fn":"search_orders","args":{"order_id":"12345"},"result":{"status":"shipped"},"started_at":1710000000.123,"ended_at":1710000000.456}
{"id":"c3d4...","seq":1,"fn":"get_shipping_details","args":{"tracking":"TRK-789"},"result":{"carrier":"FedEx"},"started_at":1710000000.500,"ended_at":1710000000.650}
```

JSONL is chosen because:

- **Append-only**: New traces are appended without reading or modifying existing data.
- **Line-based**: Easy to count lines (for checkpointing) and parse incrementally.
- **Atomic writes**: Each `write()` call writes exactly one line, minimizing partial-write risk.

## Checkpoint-Based Correlation

Highbar does not inject request IDs into traces. Instead, it uses line counting to correlate traces to tests:

```
Before test:  file has 5 lines    → checkpoint = 5
During test:  agent writes 3 traces → file has 8 lines
After test:   collect(since=5)    → returns lines 6, 7, 8 → 3 Trace objects
```

This approach works because:

1. Tests run sequentially -- only one trigger is active at a time.
2. The file is append-only -- new lines always appear at the end.
3. The settle time ensures all traces are written before collection.

## Error Traces

If a `@hb.tool`-decorated function raises an exception, the trace box includes the `error` field with the exception message. The exception is then re-raised, allowing the agent to handle it normally.

```json
{
  "id": "e5f6g7h8i9j0k1l2",
  "seq": 2,
  "fn": "charge_credit_card",
  "args": {"amount": 99.99, "card_token": "tok_expired"},
  "result": null,
  "started_at": 1710000001.100,
  "ended_at": 1710000001.150,
  "error": "CardExpiredError: The card has expired"
}
```

## Thread Safety

The `TraceWriter` serializes all writes through a `threading.Lock`. This ensures:

- The sequence counter increments atomically.
- File writes do not interleave when multiple threads call `@hb.tool` functions concurrently.
- Each trace box is a complete, well-formed JSON line.
