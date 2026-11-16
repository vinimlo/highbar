"""examples/prompt-loop end to end: loop.py runs the real CLI against the example agent.

Each test copies the example, moves its agent to a free port, and runs one cycle: the
baseline prompt in 3 rounds, then each candidate in 3 rounds, then `highbar compare`.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.quickstart import free_port

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "prompt-loop"
EXAMPLE_PORT = 8734
HIGHBAR = f"{shlex.quote(sys.executable)} -m highbar.cli.main"

REFUND = "Refunds/refunds an order after looking it up"
ORDER_ID = "Refunds/never refunds without an order id"
ANGRY = "Escalation/hands an angry customer to a human"


@pytest.fixture
def example(tmp_path: Path) -> Path:
    """The example in a folder of its own, its agent on a free port, prompt.md its prompt."""
    folder = tmp_path / "prompt-loop"
    folder.mkdir()
    for name in ("agent.py", "loop.py"):
        shutil.copy(EXAMPLE / name, folder / name)
    for name in ("specs", "prompts"):
        shutil.copytree(EXAMPLE / name, folder / name)
    port = str(free_port())
    for relative in ("agent.py", "specs/prompt-loop.highbar.yaml"):
        path = folder / relative
        text = path.read_text(encoding="utf-8")
        assert str(EXAMPLE_PORT) in text, f"{relative} no longer says {EXAMPLE_PORT}"
        path.write_text(text.replace(str(EXAMPLE_PORT), port), encoding="utf-8")
    shutil.copy(folder / "prompts" / "baseline.md", folder / "prompt.md")
    return folder


def _loop(folder: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "loop.py", "--highbar", HIGHBAR, "--prompt", "prompt.md", *args],
        cwd=folder,
        env={**os.environ, "COLUMNS": "200"},
        capture_output=True,
        text=True,
        timeout=600,
    )


def _prompt(folder: Path, name: str) -> str:
    return (folder / "prompts" / name).read_text(encoding="utf-8")


@pytest.mark.timeout(600)
def test_the_fix_is_adopted_and_the_regression_is_rejected(example: Path) -> None:
    older = "# Experience\n\nAn older entry, which the loop must keep.\n"
    (example / "EXPERIENCE.md").write_text(older, encoding="utf-8")

    result = _loop(
        example,
        "prompts/fix-order-id.md",
        "prompts/hand-off-more.md",
        "--max-cost",
        "0.01",
        "--cycle-max-cost",
        "0.2",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert (example / "prompt.md").read_text(encoding="utf-8") == _prompt(
        example, "fix-order-id.md"
    )
    assert "adopted prompts/fix-order-id.md" in output
    # The regressing candidate passes more tests than the baseline: only compare stops it.
    assert "prompts/hand-off-more.md: 3 of 4 tests passed" in output
    lost = f"lost a test that passed in every baseline round: {REFUND}"
    regression = f"highbar compare found a regression: {REFUND}"
    assert f"rejected prompts/hand-off-more.md: {lost}; {regression}" in output
    assert re.search(rf"✗ worse\s+{re.escape(REFUND)}", output), output

    experience = (example / "EXPERIENCE.md").read_text(encoding="utf-8")
    assert experience.startswith(older)
    assert ORDER_ID in experience and ANGRY in experience  # the baseline's failures
    assert "### prompts/fix-order-id.md: adopted" in experience
    assert "### prompts/hand-off-more.md: rejected" in experience
    assert "highbar compare found a regression" in experience
    assert "3/4 tests passed (baseline 2/4)" in experience
    assert "no test that passed in the baseline was lost" in experience
    assert "found no regression" not in experience
    assert re.search(rf"✓ better\s+{re.escape(ORDER_ID)}", experience), experience
    assert re.search(rf"✗ worse\s+{re.escape(REFUND)}", experience), experience
    for report in re.findall(r"`([^`]+\.json)`", experience):
        assert (example / report).is_file(), report


@pytest.mark.timeout(600)
def test_a_cycle_ceiling_too_low_skips_runs_and_adopts_nothing(example: Path) -> None:
    result = _loop(
        example,
        "prompts/fix-order-id.md",
        "--max-cost",
        "0.01",
        "--cycle-max-cost",
        "0.01",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert (example / "prompt.md").read_text(encoding="utf-8") == _prompt(example, "baseline.md")
    assert "baseline round 2/3: not started" in output
    assert "nothing adopted" in output
    assert len(list((example / "loop-runs").glob("*/baseline/*.json"))) == 1
    experience = (example / "EXPERIENCE.md").read_text(encoding="utf-8")
    assert "the cycle ceiling" in experience


@pytest.mark.timeout(600)
def test_an_error_blocks_adoption(example: Path) -> None:
    """The candidate passes more tests than the baseline, but one test is an error."""
    billing = example / "prompts" / "billing.md"
    billing.write_text(
        _prompt(example, "fix-order-id.md") + "Hand angry customers to the billing team.\n",
        encoding="utf-8",
    )

    result = _loop(example, "prompts/billing.md", "--max-cost", "0.01", "--cycle-max-cost", "0.2")

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert (example / "prompt.md").read_text(encoding="utf-8") == _prompt(example, "baseline.md")
    assert "prompts/billing.md: 3 of 4 tests passed" in output
    assert f"rejected prompts/billing.md: {ANGRY} ended in error in round 1" in output
    experience = (example / "EXPERIENCE.md").read_text(encoding="utf-8")
    assert "### prompts/billing.md: rejected" in experience
    assert "ended in error" in experience


@pytest.mark.timeout(600)
def test_propose_writes_the_candidate_and_dry_run_only_prints_it(example: Path) -> None:
    proposer = example / "propose.py"
    proposer.write_text(
        "import json, shutil, sys\n"
        "report, prompt, out = sys.argv[1:]\n"
        "json.load(open(report))\n"
        "assert open(prompt).read().startswith('You are')\n"
        "shutil.copy('prompts/fix-order-id.md', out + '/proposed.md')\n",
        encoding="utf-8",
    )

    result = _loop(
        example,
        "--propose",
        f"{shlex.quote(sys.executable)} propose.py",
        "--dry-run",
        "--max-cost",
        "0.01",
        "--cycle-max-cost",
        "0.2",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert (example / "prompt.md").read_text(encoding="utf-8") == _prompt(example, "baseline.md")
    assert "dry run: prompt.md left as it was" in output
    assert "Never refund without an order id." in output  # the adopted prompt, printed
    experience = (example / "EXPERIENCE.md").read_text(encoding="utf-8")
    assert "proposed.md: adopted (dry run, not copied)" in experience


@pytest.mark.timeout(600)
def test_f1_with_one_run_a_round_the_lost_test_still_rejects(example: Path) -> None:
    """Review of #101, F1: 3 executions a side are too few for compare's verdict, so it
    exited 0, and the candidate that breaks the refund test was adopted."""
    spec = example / "specs" / "prompt-loop.highbar.yaml"
    text = spec.read_text(encoding="utf-8")
    assert "defaults:\n  runs: 3\n" in text
    spec.write_text(text.replace("defaults:\n  runs: 3\n", "defaults:\n  runs: 1\n"), "utf-8")

    result = _loop(
        example, "prompts/hand-off-more.md", "--max-cost", "0.01", "--cycle-max-cost", "0.2"
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert (example / "prompt.md").read_text(encoding="utf-8") == _prompt(example, "baseline.md")
    assert "0 worse" in output  # compare could not tell
    lost = "lost a test that passed in every baseline round"
    assert f"rejected prompts/hand-off-more.md: {lost}: {REFUND}" in output
