# Prompt loop

A loop that tries changes to an agent's system prompt and adopts one only when highbar's results support it: the suite passes more tests, no test that passed in the baseline is lost, `highbar compare` calls no test `worse`, and nothing ended in `error`. No model judges anything. It runs offline: the agent has no model and needs no key.

```console
pip install highbar
cp prompts/baseline.md prompt.md
python loop.py --prompt prompt.md prompts/fix-order-id.md prompts/hand-off-more.md \
    --max-cost 0.01 --cycle-max-cost 0.1
```

## The files

- `agent.py`: a support agent on the standard library's HTTP server, with the highbar SDK recording its tool calls and one turn per message. It reads its system prompt from the file in `PROMPT_FILE`. **A real agent sends that prompt to a model.** This one looks for a few sentences it knows ("Never refund without an order id.", "Hand angry customers to the support team.", ...) and follows them, so a change to the prompt changes what it does, the same way on every run.
- `specs/prompt-loop.highbar.yaml`: four tests. `prompts/baseline.md` fails two of them: it refunds when the customer gives no order id, and it does not hand an angry customer to a person.
- `prompts/fix-order-id.md`: the baseline plus "Never refund without an order id." It fixes one failure and breaks nothing.
- `prompts/hand-off-more.md`: hands angry customers and every refund to a person. It fixes both failures and breaks the refund the baseline got right: 3 tests of 4, one more than the baseline, and still a regression.
- `loop.py`: the loop, standard library only. It calls the `highbar` command.

## One cycle

1. **The baseline.** The suite runs with the current prompt (`--prompt`), `--rounds` times (3 by default): `highbar run specs --report json=loop-runs/<cycle>/baseline/1.json --max-cost 0.01`, and so on. The prompt reaches the agent through `PROMPT_FILE` (`--prompt-env` names another variable).
2. **The candidates.** One or two prompt files, given as arguments or written by `--propose CMD` (below).
3. **The candidates' runs.** The same suite, the same rounds, with each candidate as the prompt.
4. **The comparison.** `highbar compare --base loop-runs/<cycle>/baseline --head loop-runs/<cycle>/candidate-1 --fail-on-regression`, for each candidate. Each report is one round, and compare gives no verdict with fewer than 3 rounds a side: that is why the loop runs rounds and refuses `--rounds` below 3, and why each test runs 3 times a round (`defaults.runs`). Even then, compare can lack the power to call a change: 3 executions a side, with `runs: 1`, give it nothing to say, and it exits 0 with `no verdict` on every test. Its exit 0 is therefore never read as "no regression".
5. **The decision.** A candidate is adoptable only if every test that passed in every baseline round also passed in every one of its rounds (a check that needs no statistics, so it holds whatever compare could tell), compare found no test `worse`, it passed more tests than the baseline (a test counts when it passed in every round), and no test ended in `error` on either side. A run that could not finish (exit 2), that `--max-cost` stopped (exit 3) or that `--max-errors` stopped (exit 4) makes its side not adoptable, and so does a round the cycle ceiling skipped. Among the adoptable candidates, the one with the most passes wins; on a tie, the first. Adopting copies it over the current prompt; with `--dry-run` it is printed and nothing is copied.
6. **The record.** The cycle is appended to `EXPERIENCE.md` (`--experience`): the date, the baseline's failures, each candidate with its decision and why, and the evidence: the pass counts, compare's verdict lines, and the paths of every report.

The cost: each run gets `--max-cost`, which highbar holds. The loop adds up what each run spent, from its report, and does not start a run when the spend so far plus `--max-cost` would pass `--cycle-max-cost`. A run whose cost the report does not know (the agent reports no usage, or costs in two currencies) is counted at its `--max-cost`.

The loop exits 0 when it adopted a candidate, 1 when it adopted none, and 2 when the cycle could not run.

## A run

The output of the command above, on the example:

```text
prompt-loop: specs, 3 rounds a side, --max-cost 0.01 a run, 0.1 for the cycle · loop-runs/20261116-233841-270689

baseline (prompt.md)
  baseline round 1/3: exit 1 · 2 of 4 tests passed · 0.002088 USD · loop-runs/20261116-233841-270689/baseline/1.json
  baseline round 2/3: exit 1 · 2 of 4 tests passed · 0.002088 USD · loop-runs/20261116-233841-270689/baseline/2.json
  baseline round 3/3: exit 1 · 2 of 4 tests passed · 0.002088 USD · loop-runs/20261116-233841-270689/baseline/3.json

candidate 1 (prompts/fix-order-id.md)
  candidate-1 round 1/3: exit 1 · 3 of 4 tests passed · 0.002709 USD · loop-runs/20261116-233841-270689/candidate-1/1.json
  candidate-1 round 2/3: exit 1 · 3 of 4 tests passed · 0.002709 USD · loop-runs/20261116-233841-270689/candidate-1/2.json
  candidate-1 round 3/3: exit 1 · 3 of 4 tests passed · 0.002709 USD · loop-runs/20261116-233841-270689/candidate-1/3.json
  compare: exit 0
    ✓ better  Refunds/never refunds without an order id  base 0/9 in 3 rounds (0%–30%) → head 9/9 in 3 rounds (70%–100%) · p 4.11e-05 · Holm p 0.000165
    4 compared | 1 better | 0 worse | 3 no verdict
  prompts/fix-order-id.md: 3 of 4 tests passed

candidate 2 (prompts/hand-off-more.md)
  candidate-2 round 1/3: exit 1 · 3 of 4 tests passed · 0.003411 USD · loop-runs/20261116-233841-270689/candidate-2/1.json
  candidate-2 round 2/3: exit 1 · 3 of 4 tests passed · 0.003411 USD · loop-runs/20261116-233841-270689/candidate-2/2.json
  candidate-2 round 3/3: exit 1 · 3 of 4 tests passed · 0.003411 USD · loop-runs/20261116-233841-270689/candidate-2/3.json
  compare: exit 1
    ✓ better  Escalation/hands an angry customer to a human  base 0/9 in 3 rounds (0%–30%) → head 9/9 in 3 rounds (70%–100%) · p 4.11e-05 · Holm p 0.000165
    ✓ better  Refunds/never refunds without an order id  base 0/9 in 3 rounds (0%–30%) → head 9/9 in 3 rounds (70%–100%) · p 4.11e-05 · Holm p 0.000165
    ✗ worse  Refunds/refunds an order after looking it up  base 9/9 in 3 rounds (70%–100%) → head 0/9 in 3 rounds (0%–30%) · p 4.11e-05 · Holm p 0.000165
    4 compared | 2 better | 1 worse | 1 no verdict
  prompts/hand-off-more.md: 3 of 4 tests passed

rejected prompts/hand-off-more.md: lost a test that passed in every baseline round: Refunds/refunds an order after looking it up; highbar compare found a regression: Refunds/refunds an order after looking it up
adopted prompts/fix-order-id.md: 3 of 4 tests passed, the baseline 2; no test that passed in the baseline was lost; highbar compare: 1 better, 0 worse, 3 no verdict
copied over prompt.md
spent 0.02462 USD of 0.1 in 9 runs
EXPERIENCE.md: cycle appended
```

`prompt.md` is now `fix-order-id.md`, and `EXPERIENCE.md` holds the cycle. Run the loop again with the next candidates, and the new prompt is the baseline.

## `--propose CMD`

In place of candidate files, `--propose` names a command that writes them: an editor, a script, or a model that drafts prompts. After the baseline's rounds, the loop runs

```text
CMD BASELINE_REPORT CURRENT_PROMPT OUT_DIR
```

with the JSON report of the baseline's first round, the current prompt's path, and an empty folder. The command writes 1 or 2 files in `OUT_DIR`, and exits 0. They are the candidates, in the order of their names. Any other exit, or no file, or more than 2, ends the cycle with exit 2 and nothing adopted. The command only proposes: what it writes goes through the same runs, the same comparison and the same rule as a file given by hand.

## Why no model judges the result

The idea comes from the Harness² paper: a loop that proposes changes to an agent and keeps the ones that do better. There, a model judged which output was better, and with no ground truth to check against it could pick an output scoring 55 over one scoring 95. Here a model may write the candidates (`--propose`), but the decision is the suite's: tests that say what the agent must do, a pass count, and a comparison whose verdict holds only when the difference is beyond chance across rounds. A candidate that fixes two tests and breaks a third looks better by its count, and two checks catch it: the test it lost, and compare calling that test `worse`. The adopted candidate's reason says only what was established: no test was lost, and how many tests compare called better, worse, or could not tell.

## Why this is an example and not a command

The loop is a few calls to `highbar run` and `highbar compare` and a rule on their results. It stays an example, for teams to copy and change (the rule, the rounds, how the prompt reaches the agent), and becomes a highbar command only if teams repeat this cycle the same way.
