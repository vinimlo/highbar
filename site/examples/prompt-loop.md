# Prompt loop

A script that tries changes to an agent's system prompt and adopts one only when highbar's results support it. The source, with a run's full output, is [`examples/prompt-loop`](https://github.com/vinimlo/highbar/tree/main/examples/prompt-loop).

```console
cp prompts/baseline.md prompt.md
python loop.py --prompt prompt.md prompts/fix-order-id.md prompts/hand-off-more.md \
    --max-cost 0.01 --cycle-max-cost 0.1
```

The agent has no model: it reads its prompt for a few sentences it knows and follows them, so the example runs offline and gives the same result every time. A real agent sends the prompt to a model.

## One cycle

1. The suite runs with the current prompt in 3 rounds, each a `highbar run --report json=... --max-cost ...`.
2. One or two candidate prompts come from the command line, or from `--propose CMD`, which writes them.
3. The suite runs with each candidate in as many rounds.
4. `highbar compare --fail-on-regression` sets each candidate's rounds against the baseline's. Compare gives a verdict only with 3 rounds or more a side, which is why the loop runs rounds and refuses `--rounds` below 3. With few executions it can still lack the power to tell, and exits 0 with `no verdict`: that is not read as "no regression".
5. A candidate is adopted only if every test that passed in every baseline round still passes in every round, compare found no test `worse`, it passed more tests than the baseline, and no test ended in `error` on either side. A run that could not finish, or that `--max-cost` stopped, is never adopted. The candidate with the most passes wins; on a tie, the first.
6. The cycle goes into `EXPERIENCE.md`: the baseline's failures, each candidate's decision and why, and the evidence (pass counts, compare's verdict lines, report paths).

The loop adds up what each run spent, from its report, and does not start a run that could take the cycle past `--cycle-max-cost`.

In the example, `hand-off-more.md` passes 3 tests of 4, one more than the baseline, and is rejected: it breaks a refund the baseline got right, which the loop sees directly, and compare calls that test `worse`. `fix-order-id.md` also passes 3, breaks nothing, and is adopted.

## Why no model judges

A model may write the candidates, but the decision is the suite's: tests that say what the agent must do, and a comparison whose verdict holds only beyond chance. A model asked which of two outputs is better, with nothing to check them against, can pick the worse one. A count of passes can mislead too, which is why a test the baseline passed must not be lost, whatever compare could tell.

See [comparing two configurations](/guides/comparing) for how compare decides.
