# Repetition and CI gates

An agent built on an LLM gives different answers to the same message. A test that ran once and passed may pass two times in five. `runs` sends the message several times, each as its own execution, and `min_pass` says how many must pass.

```yaml test
name: cancels when asked
trigger: "I want to cancel my order"
runs: 5
min_pass: 4
expect:
  function_calls:
    - name: cancel_order
```

The title shows the count: `✓ cancels when asked 5/5 (min 4)` or `✗ cancels when asked 3/5 (min 4)`. A failure that repeats across runs is shown once, with how many runs it hit: `(2 of 5 runs)`. It is one line too when what the agent did changed between runs, a reply or the calls it made, and the line is the first run it failed in: `Response does not contain 'refund'. Got: 'I can't help with that' (3 of 5 runs; as in run 2)`, or `Expected no function calls, but got: search (2 of 5 runs; as in run 1)`. The JSON report keeps each run's own message.

## `min_pass`

- A whole number is a count of runs: `min_pass: 4` of 5.
- A fraction is a share of the runs, rounded up: `min_pass: 0.8` of 5 is 4, and of 3 is 3.
- `1` and `1.0` are different: `1` is one run, `1.0` is every run.
- Without `min_pass`, the test takes `defaults.min_pass`. Without either, every run must pass.
- A `min_pass` above the number of runs is an error before anything runs (exit code 2).

## One threshold for every test

When most tests share a threshold, state it once in `defaults`:

```yaml
agent:
  start: "python agent.py"
  endpoint: "http://127.0.0.1:8731/chat"

defaults:
  runs: 3
  min_pass: 0.66

suites:
  - name: Orders
    tests:
      - name: looks the order up
        trigger: "Where is order 1042?"
        expect:
          function_calls:
            - name: get_order
      - name: cancels when asked
        trigger: "I want to cancel order 1042"
        runs: 5
        expect:
          function_calls:
            - name: cancel_order
      - name: never refunds without asking
        trigger: "Order 1042 arrived broken"
        min_pass: 1.0
        expect:
          forbid: [refund_order]
```

`defaults.min_pass` takes the same values as a test's `min_pass`, and each test uses the first of these that is set:

1. the test's own `min_pass`;
2. `defaults: {min_pass: N}` in the spec;
3. every run.

A test with its own `runs` and no `min_pass` still takes `defaults.min_pass`, counted on its own runs. Above, the first test must pass 2 of 3, the second 4 of 5, and the third every run.

A fraction is a share of each test's runs, so `--runs 10` makes `min_pass: 0.66` mean 7 of 10. A whole number stays the same whatever the runs: `defaults: {min_pass: 3}` is 3 of 5 with `--runs 5`, and an error, before anything runs, for any test with fewer than 3 runs. The message names the test and says the minimum came from `defaults.min_pass`.

## How many runs

The first of these that is set wins:

1. the test's own `runs`;
2. `--runs N` on the command line (or `highbar.run(spec, runs=N)`);
3. `defaults: {runs: N}` in the spec;
4. one run.

So a spec can keep `runs: 1` for a fast local loop, and CI can run everything three times with `--runs 3`, while a test that needs ten runs says so itself.

## Runs that could not be checked

A run that ended in `error` (the agent timed out, the message was not observed) counts as not passed. It is not evidence that the agent did wrong, either. When the runs that passed plus the runs that errored could have reached `min_pass`, the test is an `error`, not a `failed`: 3 passed and 2 timed out with `min_pass: 4` is unverified. Both are red, and both exit with 1.

Errors that come now and then are what `min_pass` is for. Errors that never stop are not: when 5 executions in a row get no reply for the same cause (the same HTTP status, a refused connection, a timeout or an empty body, from the same endpoint), the infrastructure is down, and the run stops there with exit code 4, even in the middle of a test's runs. The test it stopped in is never a pass: it is `failed` when a failure is already known (a phrasing that ended failed, or one whose failures alone keep it under `min_pass` whatever the runs it did not make), and an `error` otherwise. Any other ending starts the count over. [`--max-errors`](/reference/cli#max-errors) changes the 5, and `--max-errors 0` turns the stop off.

A tool call that arrives after its run was judged (a background task that finished late) fails that run, and the test is judged again against `min_pass`. When the test was already reported, highbar prints a warning, and counts it again if its result changed.

## Many runs in less time

Ten runs of fifty tests are five hundred messages, and most of their time is the agent thinking. `highbar run --jobs 4` keeps up to four tests in flight at once, so the run takes about a quarter of the time. Each test's runs still go one after the other, so `min_pass` and the conversation of a multi-turn test are as before, and the results come out in spec order.

Two things change with more than one test in flight:

- Every record must carry the test's correlation (`agent.correlate`, the test id by default): the window a record falls in no longer says whose it is. A test with no record of its own is an `error`, never a pass on records it happened to see. Check that your agent tags everything with `--jobs 1` first: the warning about calls without a test context says what is missing.
- Session hooks run for up to N tests at once, and one test's setup can come before another's teardown. A Python hook must be thread-safe, and must not keep state that assumes one test at a time.

With `--max-cost`, the tests in flight when the ceiling is passed still finish, and the message says how much more they spent. See [`--jobs`](/reference/cli#jobs).

## A CI gate

```yaml workflow
# .github/workflows/evals.yml
name: evals
on: pull_request
jobs:
  evals:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install highbar -r requirements.txt
      - run: highbar run --frozen --runs 3
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}  # what your agent needs
```

- `--runs 3` is a reasonable floor for a gate; raise it for the tests that decide a release.
- `--frozen` makes the run use only the committed [variations](/guides/variations), so it never calls an LLM to paraphrase.
- The exit code is the gate: 0 when every test passed, 1 when any failed or errored, 2 when nothing could run, 3 when `--max-cost` stopped the run, 4 when `--max-errors` stopped it because the agent's infrastructure kept failing.
