# Comparing two configurations

To tell whether a change made the agent better (a flag, a prompt, a model), run the same specs on each side with `--report json=` several times, and compare the reports:

```bash
for i in 1 2 3 4; do HIGHBAR_FLAG=off highbar run --runs 5 --report json=runs/off/$i.json; done
for i in 1 2 3 4; do HIGHBAR_FLAG=on  highbar run --runs 5 --report json=runs/on/$i.json; done
highbar compare --base runs/off --head runs/on
```

Each report is one round. `compare` pairs the tests by id across every report and says, for each test, how many executions passed on each side, with a 95% interval. It says `better` or `worse` only when both of these hold:

- the p-value, adjusted with Holm across every test compared, is under 0.05. Seven tests are seven comparisons: a raw p of 0.048 on one of them is what chance gives about once in three such runs;
- each side has at least 3 rounds.

The p-value is the larger of two tests. Fisher's exact test on the counts holds when every execution is independent. When an agent's choice sticks within a round (1 of 5 in one round, 5 of 5 in the next), the executions are not independent and only the rounds repeat, so Welch's test on the rounds' pass rates holds instead. Taking the larger holds either way. With the same agent on both sides and rounds that vary, the counts alone called a difference up to 19% of the time; this rule, under 5%. When the rounds of a side disagree and there is no verdict, the output says so.

The price is power with few rounds. With rounds that do not vary, a real drop from 90% to 60% is called `worse` about 16% of the time with 3 rounds of 5, 37% with 4 of 5, 40% with 3 of 10, and 85% with 5 of 10. More rounds buy more than longer ones. A round with no execution of a test does not count as one of its rounds.

Otherwise it shows the numbers and why there is no verdict. Four rounds of 5 tell more than one round of 20, because only they measure how much a round varies.

A test whose definition changed between reports (its trigger, turns or expectation) is listed and not compared. Executions that ended in `error` count as not passed, as they do for `min_pass`, and are named apart: left out, a side whose agent stops answering would look no worse. An error never makes the head look better, though: when the head is ahead only because the base's executions ended in `error`, there is no verdict, so a base that errored everywhere is not beaten. A report given twice, in a folder and by name, or copied (even written again with other spacing), is refused, since one run would count as two rounds. In `--report json=`, a side's `executions` counts every execution, `error` ones included. Cost, tokens and time per execution are shown on both sides, with no test.

`--fail-on-regression` makes `worse` exit with 1, for a CI gate on a change. A raw p that does not survive Holm or the round check never fires it. `--report json=FILE` writes the comparison for another tool.
