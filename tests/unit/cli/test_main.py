"""Tests for the CLI commands that don't need a running agent."""

from __future__ import annotations

import json
import sys
import textwrap
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from highbar.cli.main import _discover_specs, app, console
from highbar.domain.models import BUDGET_EXCEEDED, REPEATED_ERRORS, RunSummary
from highbar.engine.run_spec import agent_log_path
from highbar.reporters.json_report import ReportError

runner = CliRunner()

SPEC = """\
agent:
  start: "true"
  endpoint: "http://localhost:8000/chat"
suites:
  - name: Menu
    tests:
      - trigger: "me mostra o [cardápio]"
        variations: 2
        expect:
          function_calls:
            - name: list_categories
"""


class _Response:
    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"choices": [{"message": {"content": self._content}}]}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project directory with one spec under specs/, used as the cwd."""
    specs = tmp_path / "specs" / "menu"
    specs.mkdir(parents=True)
    (specs / "menu.highbar.yaml").write_text(textwrap.dedent(SPEC), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIGHBAR_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("COLUMNS", "200")
    return tmp_path


class TestDiscoverSpecs:
    def test_a_file_is_returned_as_is(self, project: Path) -> None:
        assert _discover_specs("specs/menu/menu.highbar.yaml") == ["specs/menu/menu.highbar.yaml"]

    def test_a_directory_is_searched(self, project: Path) -> None:
        assert _discover_specs("specs/") == ["specs/menu/menu.highbar.yaml"]

    def test_no_argument_searches_specs(self, project: Path) -> None:
        assert _discover_specs("") == ["specs/menu/menu.highbar.yaml"]


def _summary(**counts: int) -> RunSummary:
    values = {"total": 1, "passed": 0, "failed": 0, "errored": 0} | counts
    return RunSummary(duration_ms=10.0, **values)


class TestSelection:
    """HB-17: --suite and -k pick the tests before any agent starts."""

    def test_hb17_only_the_picked_tests_run(self, project: Path) -> None:
        other = project / "specs" / "menu" / "drinks.highbar.yaml"
        other.write_text(textwrap.dedent(SPEC).replace("name: Menu", "name: Drinks"), "utf-8")

        with patch("highbar.engine.run_spec.TestRunner") as fake:
            fake.return_value.run.return_value = _summary(passed=1)
            result = runner.invoke(app, ["run", "specs/", "--suite", "Drinks"])

        assert result.exit_code == 0, result.output
        (call,) = fake.return_value.run.call_args_list
        assert [suite.name for suite in call.args[0].suites] == ["Drinks"]

    def test_hb17_a_spec_left_out_still_lends_its_secrets(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An agent can echo another spec's token: it is masked in every spec of the run,
        picked or not (the security review of 5a2e6bf)."""
        monkeypatch.setenv("TOKEN_A", "secret-a-123")
        endpoint = '  endpoint: "http://localhost:8000/chat"\n'
        api = (
            textwrap.dedent(SPEC)
            .replace("name: Menu", "name: Api")
            .replace(
                endpoint, endpoint + '  request:\n    headers: {Authorization: "${TOKEN_A}"}\n'
            )
        )
        (project / "specs" / "menu" / "api.highbar.yaml").write_text(api, "utf-8")

        with patch("highbar.engine.run_spec.TestRunner") as fake:
            fake.return_value.run.return_value = _summary(passed=1)
            result = runner.invoke(app, ["run", "specs/", "--suite", "Menu"])

        assert result.exit_code == 0, result.output
        (call,) = fake.call_args_list
        assert "secret-a-123" in call.kwargs["secrets"]

    def test_hb17_a_tag_picks_its_tests(self, project: Path) -> None:
        tagged = textwrap.dedent(SPEC).replace("name: Menu", "name: Drinks\n    tags: [smoke]")
        (project / "specs" / "menu" / "drinks.highbar.yaml").write_text(tagged, "utf-8")

        with patch("highbar.engine.run_spec.TestRunner") as fake:
            fake.return_value.run.return_value = _summary(passed=1)
            result = runner.invoke(app, ["run", "specs/", "--tag", "smoke"])

        assert result.exit_code == 0, result.output
        (call,) = fake.return_value.run.call_args_list
        assert [suite.name for suite in call.args[0].suites] == ["Drinks"]

    def test_hb17_a_cut_run_says_what_it_left_out(self, project: Path) -> None:
        other = project / "specs" / "menu" / "drinks.highbar.yaml"
        other.write_text(textwrap.dedent(SPEC).replace("name: Menu", "name: Drinks"), "utf-8")
        report = project / "report.json"

        with patch("highbar.engine.run_spec.TestRunner") as fake:
            fake.return_value.run.return_value = _summary(passed=1)
            fake.return_value.snapshot.return_value = _summary(passed=1)
            result = runner.invoke(
                app, ["run", "specs/", "--suite", "Drinks", "--report", f"json={report}"]
            )

        assert result.exit_code == 0, result.output
        assert "! part of the tests: 1 of 2 picked by --suite Drinks, 1 left out" in result.output
        selection = json.loads(report.read_text(encoding="utf-8"))["selection"]
        assert selection == {
            "suites": ["Drinks"],
            "keywords": [],
            "tags": [],
            "picked": 1,
            "left_out": 1,
        }

    def test_hb17_options_that_pick_every_test_print_no_line(self, project: Path) -> None:
        """-k that matches every test cuts nothing: no "part of the tests" line, and the
        report keeps the selection, with left_out 0 (the review of 4fb326f)."""
        report = project / "report.json"

        with patch("highbar.engine.run_spec.TestRunner") as fake:
            fake.return_value.run.return_value = _summary(passed=1)
            fake.return_value.snapshot.return_value = _summary(passed=1)
            result = runner.invoke(
                app, ["run", "specs/", "-k", "menu", "--report", f"json={report}"]
            )

        assert result.exit_code == 0, result.output
        assert "part of the tests" not in result.output
        selection = json.loads(report.read_text(encoding="utf-8"))["selection"]
        assert selection == {
            "suites": [],
            "keywords": ["menu"],
            "tags": [],
            "picked": 1,
            "left_out": 0,
        }

    def test_hb17_the_line_quotes_what_it_was_given(self, project: Path) -> None:
        other = project / "specs" / "menu" / "drinks.highbar.yaml"
        other.write_text(textwrap.dedent(SPEC).replace("name: Menu", "name: Drinks"), "utf-8")

        with patch("highbar.engine.run_spec.TestRunner") as fake:
            fake.return_value.run.return_value = _summary(passed=1)
            result = runner.invoke(app, ["run", "specs/", "-k", "menu/me mostra"])

        assert result.exit_code == 0, result.output
        assert "! part of the tests: 1 of 2 picked by -k 'menu/me mostra', 1 left out" in (
            result.output
        )

    @pytest.mark.parametrize(("reason", "code"), [(REPEATED_ERRORS, 4), (BUDGET_EXCEEDED, 3)])
    def test_hb17_a_stop_tells_its_unrun_tests_from_the_ones_left_out(
        self, project: Path, reason: str, code: int
    ) -> None:
        """Three specs, --suite Menu picks two; the first stops the run, so one picked
        test did not run, and one was left out by the option (the review of 4fb326f)."""
        menu = project / "specs" / "menu"
        (menu / "other.highbar.yaml").write_text(textwrap.dedent(SPEC), "utf-8")
        drinks = textwrap.dedent(SPEC).replace("name: Menu", "name: Drinks")
        (menu / "drinks.highbar.yaml").write_text(drinks, "utf-8")
        stop = "the run stopped here"
        stopped = replace(_summary(errored=1), stopped=stop, stop_reason=reason)

        with patch("highbar.engine.run_spec.TestRunner") as fake:
            fake.return_value.run.side_effect = [stopped, _summary(passed=1)]
            result = runner.invoke(app, ["run", "specs/", "--suite", "Menu"])

        assert result.exit_code == code, result.output
        line = (
            "! part of the tests: 2 of 3 picked by --suite Menu, 1 left out; "
            "of the 2 picked, 1 did not run: the run stopped"
        )
        assert line in result.output
        assert result.output.index(line) < result.output.index(stop)

    def test_hb17_a_whole_run_says_nothing_of_the_kind(self, project: Path) -> None:
        report = project / "report.json"

        with patch("highbar.engine.run_spec.TestRunner") as fake:
            fake.return_value.run.return_value = _summary(passed=1)
            fake.return_value.snapshot.return_value = _summary(passed=1)
            result = runner.invoke(app, ["run", "specs/", "--report", f"json={report}"])

        assert result.exit_code == 0, result.output
        assert "part of the tests" not in result.output
        assert json.loads(report.read_text(encoding="utf-8"))["selection"] is None

    def test_hb17_a_suite_no_spec_has_exits_2(self, project: Path) -> None:
        with patch("highbar.engine.run_spec.TestRunner") as fake:
            result = runner.invoke(app, ["run", "specs/", "--suite", "Menus"])

        assert result.exit_code == 2, result.output
        assert "--suite 'Menus': no spec has a suite with that name" in result.output
        fake.assert_not_called()

    def test_hb17_a_selection_of_nothing_exits_2(self, project: Path) -> None:
        with patch("highbar.engine.run_spec.TestRunner") as fake:
            result = runner.invoke(app, ["run", "specs/", "-k", "zzz"])

        assert result.exit_code == 2, result.output
        assert "no test matches -k zzz: nothing would be checked" in result.output
        fake.assert_not_called()


class TestSelectionHelp:
    def test_hb17_the_help_says_how_the_options_combine(self) -> None:
        """Any value within an option, every option given (the review of #46)."""
        result = runner.invoke(app, ["run", "--help"], env={"COLUMNS": "200"})

        text = " ".join(result.output.replace("\u2502", " ").split())
        for option, others in (
            ("--suite", "-k or --tag"),
            ("-k", "--suite or --tag"),
            ("--tag", "--suite or -k"),
        ):
            assert (
                f"Given more than once, any of them; with {others}, a test must match each." in text
            ), option


class TestSelectionWithTheLlmProxy:
    """HB-17 with HB-30: a spec with no test picked starts neither its agent nor its proxy,
    and does not hold --jobs back."""

    @staticmethod
    def _proxied(project: Path) -> None:
        proxied = (
            textwrap.dedent(SPEC)
            .replace("name: Menu", "name: Drinks")
            .replace('  start: "true"\n', '  start: "drinks-agent"\n  llm_proxy: true\n')
        )
        (project / "specs" / "menu" / "drinks.highbar.yaml").write_text(proxied, "utf-8")

    @pytest.mark.parametrize("jobs", ["1", "2"])
    def test_hb17_a_proxied_spec_cut_whole_starts_no_agent_and_no_proxy(
        self, project: Path, jobs: str
    ) -> None:
        self._proxied(project)

        with (
            patch("highbar.engine.run_spec.TestRunner") as fake,
            patch("highbar.engine.run_spec.LlmProxy") as proxy,
            patch("highbar.engine.run_spec.SubprocessAdapter") as adapter,
        ):
            fake.return_value.run.return_value = _summary(passed=1)
            result = runner.invoke(app, ["run", "specs/", "--suite", "Menu", "--jobs", jobs])

        assert result.exit_code == 0, result.output
        proxy.assert_not_called()
        assert [call.kwargs["start_cmd"] for call in adapter.call_args_list] == ["true"]

    @pytest.mark.parametrize("jobs", ["1", "2"])
    def test_hb17_a_proxied_spec_cut_by_tag_starts_no_agent_and_no_proxy(
        self, project: Path, jobs: str
    ) -> None:
        self._proxied(project)
        tagged = textwrap.dedent(SPEC).replace("name: Menu", "name: Menu\n    tags: [fast]")
        (project / "specs" / "menu.highbar.yaml").write_text(tagged, "utf-8")

        with (
            patch("highbar.engine.run_spec.TestRunner") as fake,
            patch("highbar.engine.run_spec.LlmProxy") as proxy,
            patch("highbar.engine.run_spec.SubprocessAdapter") as adapter,
        ):
            fake.return_value.run.return_value = _summary(passed=1)
            result = runner.invoke(app, ["run", "specs/", "--tag", "fast", "--jobs", jobs])

        assert result.exit_code == 0, result.output
        proxy.assert_not_called()
        assert [call.kwargs["start_cmd"] for call in adapter.call_args_list] == ["true"]

    def test_hb17_jobs_over_one_still_refuses_a_picked_proxied_spec(self, project: Path) -> None:
        self._proxied(project)

        with patch("highbar.engine.run_spec.TestRunner") as fake:
            result = runner.invoke(app, ["run", "specs/", "--suite", "Drinks", "--jobs", "2"])

        assert result.exit_code == 2, result.output
        assert "agent.llm_proxy" in result.output and "--jobs" in result.output
        fake.assert_not_called()


class TestExitCodes:
    def test_version_flag(self) -> None:
        result = runner.invoke(app, ["--version"])

        assert result.exit_code == 0, result.output
        assert result.output.startswith("highbar ")

    def test_no_specs_found_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["run"])

        assert result.exit_code == 2, result.output

    def test_errored_test_exits_1(self, project: Path) -> None:
        with patch("highbar.engine.run_spec.TestRunner") as fake:
            fake.return_value.run.return_value = _summary(errored=1)
            result = runner.invoke(app, ["run", "specs/"])

        assert result.exit_code == 1, result.output

    def test_all_passed_exits_0(self, project: Path) -> None:
        with patch("highbar.engine.run_spec.TestRunner") as fake:
            fake.return_value.run.return_value = _summary(passed=1)
            result = runner.invoke(app, ["run", "specs/"])

        assert result.exit_code == 0, result.output

    def test_total_line_when_more_than_one_spec(self, project: Path) -> None:
        second = project / "specs" / "menu" / "other.highbar.yaml"
        second.write_text(textwrap.dedent(SPEC), encoding="utf-8")

        with patch("highbar.engine.run_spec.TestRunner") as fake:
            fake.return_value.run.side_effect = [_summary(passed=1), _summary(failed=1)]
            result = runner.invoke(app, ["run", "specs/"])

        assert result.exit_code == 1, result.output
        assert "Total: 1 passed | 1 failed | 0.02s" in result.output


class TestJobs:
    """HB-8: --jobs is 1 unless given, and a count under 1 exits 2 before anything runs."""

    @pytest.mark.parametrize(
        ("args", "expected"), [([], 1), (["--jobs", "3"], 3), (["--jobs=2"], 2)]
    )
    def test_hb8_the_option_reaches_the_runner(
        self, project: Path, args: list[str], expected: int
    ) -> None:
        with patch("highbar.engine.run_spec.TestRunner") as fake:
            fake.return_value.run.return_value = _summary(passed=1)
            result = runner.invoke(app, ["run", "specs/", *args])

        assert result.exit_code == 0, result.output
        assert fake.call_args.kwargs["jobs"] == expected

    @pytest.mark.parametrize("value", ["0", "-1"])
    def test_hb8_a_count_under_one_exits_2_before_anything_runs(
        self, project: Path, value: str
    ) -> None:
        report = project / "report.json"

        with patch("highbar.engine.run_spec.TestRunner") as fake:
            result = runner.invoke(
                app, ["run", "specs/", f"--jobs={value}", "--report", f"json={report}"]
            )

        assert result.exit_code == 2, result.output
        assert "--jobs must be a whole number of at least 1" in result.output
        fake.assert_not_called()
        data = json.loads(report.read_text(encoding="utf-8"))
        assert (data["exit_code"], data["stopped"]["reason"]) == (2, "usage_error")


class TestJobsWithTheLlmProxy:
    """HB-30, P9: no test id goes out on the agent's model calls yet, so with more than one
    test in flight the proxy could not tell whose call it saw."""

    def test_hb30_jobs_over_one_with_llm_proxy_exits_2_before_anything_runs(
        self, project: Path
    ) -> None:
        spec = project / "specs" / "menu" / "menu.highbar.yaml"
        spec.write_text(SPEC.replace('  start: "true"\n', '  start: "true"\n  llm_proxy: true\n'))
        report = project / "report.json"

        with patch("highbar.engine.run_spec.TestRunner") as fake:
            result = runner.invoke(
                app, ["run", "specs/", "--jobs", "2", "--report", f"json={report}"]
            )

        assert result.exit_code == 2, result.output
        assert "agent.llm_proxy" in result.output and "--jobs" in result.output
        fake.assert_not_called()
        data = json.loads(report.read_text(encoding="utf-8"))
        assert (data["exit_code"], data["stopped"]["reason"]) == (2, "usage_error")

    def test_hb30_jobs_one_with_llm_proxy_runs(self, project: Path) -> None:
        spec = project / "specs" / "menu" / "menu.highbar.yaml"
        spec.write_text(SPEC.replace('  start: "true"\n', '  start: "true"\n  llm_proxy: true\n'))

        with (
            patch("highbar.engine.run_spec.TestRunner") as fake,
            patch("highbar.engine.run_spec.LlmProxy"),
        ):
            fake.return_value.run.return_value = _summary(passed=1)
            result = runner.invoke(app, ["run", "specs/", "--jobs", "1"])

        assert result.exit_code == 0, result.output


class TestMaxErrors:
    """HB-34: --max-errors is 5 unless given, 0 turns it off; its stop exits with 4."""

    STOP = (
        "5 executions in a row failed with HTTP 500 from http://localhost:8000/chat "
        "(last: 'me mostra o [cardápio]'); the infrastructure failed, not the agent, so the "
        "run stopped. Exits with 4."
    )

    @pytest.mark.parametrize(
        ("args", "expected"),
        [
            ([], 5),
            (["--max-errors", "3"], 3),
            (["--max-errors=1"], 1),
            (["--max-errors", "0"], None),
        ],
    )
    def test_hb34_the_option_reaches_the_runner(
        self, project: Path, args: list[str], expected: int | None
    ) -> None:
        with patch("highbar.engine.run_spec.TestRunner") as fake:
            fake.return_value.run.return_value = _summary(passed=1)
            result = runner.invoke(app, ["run", "specs/", *args])

        assert result.exit_code == 0, result.output
        assert fake.call_args.kwargs["max_errors"] == expected

    @pytest.mark.parametrize("args", [["--max-errors", "-1"], ["--max-errors=-1"]])
    def test_hb34_ac11_a_negative_count_exits_2_before_anything_runs(
        self, project: Path, args: list[str]
    ) -> None:
        report = project / "report.json"

        with patch("highbar.engine.run_spec.TestRunner") as fake:
            result = runner.invoke(app, ["run", "specs/", *args, "--report", f"json={report}"])

        assert result.exit_code == 2, result.output
        assert "--max-errors must be a whole number of at least 0" in result.output
        fake.assert_not_called()
        data = json.loads(report.read_text(encoding="utf-8"))
        assert (data["exit_code"], data["stopped"]["reason"]) == (2, "usage_error")

    @pytest.mark.parametrize("value", ["abc", "2.5"])
    def test_hb34_ac11_a_count_that_is_not_a_whole_number_exits_2(
        self, project: Path, value: str
    ) -> None:
        with patch("highbar.engine.run_spec.TestRunner") as fake:
            result = runner.invoke(app, ["run", "specs/", f"--max-errors={value}"])

        assert result.exit_code == 2, result.output
        assert "--max-errors" in result.output
        fake.assert_not_called()

    def _stopping(self, project: Path, reason: str) -> tuple[int, str, dict[str, object], int]:
        """Two specs, the first stopped for ``reason``: the code, the output, the report,
        and how many specs ran."""
        second = project / "specs" / "menu" / "other.highbar.yaml"
        second.write_text(textwrap.dedent(SPEC), encoding="utf-8")
        report = project / "report.json"
        stopped = replace(_summary(errored=1), stopped=self.STOP, stop_reason=reason)

        with patch("highbar.engine.run_spec.TestRunner") as fake:
            fake.return_value.run.side_effect = [stopped, _summary(passed=1)]
            fake.return_value.snapshot.return_value = stopped
            result = runner.invoke(app, ["run", "specs/", "--report", f"json={report}"])

        data = json.loads(report.read_text(encoding="utf-8"))
        return result.exit_code, result.output, data, fake.return_value.run.call_count

    def test_hb34_ac10_repeated_errors_exit_4_and_the_specs_left_do_not_run(
        self, project: Path
    ) -> None:
        code, output, data, ran = self._stopping(project, REPEATED_ERRORS)

        assert code == 4, output
        assert ran == 1
        assert "5 executions in a row failed with HTTP 500" in output
        assert "(last: 'me mostra o [cardápio]')" in output
        assert data["exit_code"] == 4
        assert data["stopped"] == {"reason": "repeated_errors", "message": self.STOP}
        assert data["complete"] is False

    def test_hb34_ac10_a_ceiling_still_exits_3_and_the_specs_left_do_not_run(
        self, project: Path
    ) -> None:
        code, output, data, ran = self._stopping(project, BUDGET_EXCEEDED)

        assert code == 3, output
        assert ran == 1
        assert data["stopped"]["reason"] == "budget_exceeded"
        assert data["exit_code"] == 3


class TestSpecErrors:
    """A spec that cannot run is one line and exit code 2, before any agent starts."""

    def test_run_missing_file_exits_2_without_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["run", "nope.highbar.yaml"])

        assert result.exit_code == 2, result.output
        assert "no such file" in result.output
        assert "Traceback" not in result.output

    def test_run_invalid_spec_exits_2_before_starting_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        start = f"{sys.executable} -c \"open('MARKER', 'w')\""
        (tmp_path / "bad.highbar.yaml").write_text(
            textwrap.dedent(f"""\
                agent:
                  start: {json.dumps(start)}
                  endpoint: "http://localhost:8000/chat"
                  startup_timeout: 1000
                suites:
                  - name: S
                    tests:
                      - trigger: "hi"
                        expect:
                          responce: {{contains: "hello"}}
                """),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["run", "bad.highbar.yaml"])

        assert result.exit_code == 2, result.output
        assert "did you mean 'response'" in result.output
        assert not (tmp_path / "MARKER").exists()

    def test_validate_accepts_directory(self, project: Path) -> None:
        bad = project / "specs" / "bad.highbar.yaml"
        bad.write_text(textwrap.dedent(SPEC).replace("function_calls:", "function_call:"))

        result = runner.invoke(app, ["validate", "specs/"])

        assert result.exit_code == 2, result.output
        lines = result.output.splitlines()
        assert any("✓" in line and "menu.highbar.yaml" in line for line in lines)
        assert any("✗" in line and "bad.highbar.yaml" in line for line in lines)


class TestAgentLogPath:
    def test_does_not_reuse_an_existing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two specs named agent.highbar.yaml in one second shared a log, and a tail."""
        monkeypatch.chdir(tmp_path)
        first = agent_log_path("specs/a/agent.highbar.yaml")
        first.parent.mkdir(parents=True)
        first.touch()

        second = agent_log_path("specs/b/agent.highbar.yaml")

        assert second != first
        assert second.name.startswith("agent-")


class TestVariationsCommand:
    def test_no_specs_found_exits_2(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(app, ["variations"])

        assert result.exit_code == 2, result.output

    @pytest.mark.parametrize("argument", [["specs/"], [], ["specs/menu/menu.highbar.yaml"]])
    def test_generates_and_writes_the_cache(self, project: Path, argument: list[str]) -> None:
        """`highbar variations specs/` is the command the README documents."""
        reply = '["qual é o cardápio?", "tem [menu] aí?"]'

        with patch("httpx.post", return_value=_Response(reply)):
            result = runner.invoke(app, ["variations", *argument])

        assert result.exit_code == 0, result.output
        # Square brackets in user text survive Rich's markup.
        assert "me mostra o [cardápio]" in result.output
        assert "tem [menu] aí?" in result.output
        assert "1 generated | 0 already cached" in result.output

        cache_file = project / ".highbar" / "variations.json"
        (entry,) = json.loads(cache_file.read_text(encoding="utf-8")).values()
        assert entry["variations"] == ["qual é o cardápio?", "tem [menu] aí?"]

    def test_second_run_reuses_the_cache(self, project: Path) -> None:
        with patch("httpx.post", return_value=_Response('["a", "b"]')):
            runner.invoke(app, ["variations"])
        with patch("httpx.post") as post:
            result = runner.invoke(app, ["variations"])

        assert result.exit_code == 0, result.output
        assert "0 generated | 1 already cached" in result.output
        post.assert_not_called()

    def test_fewer_than_requested_is_reported(self, project: Path) -> None:
        """Duplicates collapse to one entry, and the report has to say so."""
        with patch("httpx.post", return_value=_Response('["a", "A", "a"]')):
            result = runner.invoke(app, ["variations"])

        assert result.exit_code == 0, result.output
        assert "1 of 2 requested" in result.output
        assert "1 with fewer than requested" in result.output

        with patch("httpx.post") as post:
            again = runner.invoke(app, ["variations"])

        assert "cached with 1 of 2" in again.output
        post.assert_not_called()

    def test_broken_cache_file_is_a_clean_error(self, project: Path) -> None:
        cache_file = project / ".highbar" / "variations.json"
        cache_file.parent.mkdir()
        cache_file.write_text("<<<<<<< HEAD\n{}\n")

        result = runner.invoke(app, ["variations"])

        assert result.exit_code == 1
        assert "not valid JSON" in result.output
        assert "Traceback" not in result.output

    def test_missing_api_key_is_a_clean_error(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HIGHBAR_LLM_API_KEY")

        result = runner.invoke(app, ["variations"])

        assert result.exit_code == 1
        assert "HIGHBAR_LLM_API_KEY" in result.output


class TestReportOption:
    def test_j13_crash_is_reported_and_exits_1(self, project: Path) -> None:
        """A bug in highbar keeps its traceback and code; the file tells it from a red test."""
        report = project / "report.json"

        with patch("highbar.cli.main.run_spec", side_effect=RuntimeError("boom")):
            result = runner.invoke(app, ["run", "--report", f"json={report}"])

        assert result.exit_code == 1
        assert isinstance(result.exception, RuntimeError)
        data = json.loads(report.read_text(encoding="utf-8"))
        assert data["exit_code"] == 1
        assert data["complete"] is False
        assert data["stopped"] == {"reason": "crashed", "message": "RuntimeError: boom"}

    def test_final_write_failure_exits_2(self, project: Path) -> None:
        """A green run without the file the CI counts on is not a 0."""
        report = project / "report.json"

        with (
            patch("highbar.cli.main.run_spec", return_value=RunSummary(1, 1, 0, 0, 1.0)),
            patch("highbar.cli.main.JsonReport.finish", side_effect=ReportError("cannot write it")),
        ):
            result = runner.invoke(app, ["run", "--report", f"json={report}"])

        assert result.exit_code == 2
        assert "cannot write it" in result.stdout

    def test_i2_a_closed_stdout_cannot_change_the_code_the_file_states(self, project: Path) -> None:
        """rich exits 1 on a broken pipe, and `Report:` is printed after the file is final."""
        report = project / "report.json"
        real = console.print

        def gone(*objects: object, **options: object) -> None:
            if any("Report:" in str(item) for item in objects):
                raise SystemExit(1)
            real(*objects, **options)

        with (
            patch("highbar.cli.main.run_spec", return_value=RunSummary(1, 1, 0, 0, 1.0)),
            patch("highbar.cli.main.console.print", side_effect=gone),
        ):
            result = runner.invoke(app, ["run", "--report", f"json={report}"])

        assert result.exit_code == 0
        assert json.loads(report.read_text(encoding="utf-8"))["exit_code"] == 0
