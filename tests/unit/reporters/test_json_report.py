"""The report file: valid on every way out, never mistaken for an older run (spec D19)."""

from __future__ import annotations

import json
import math
import os
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from highbar.domain.models import (
    AgentConfig,
    Counts,
    ExecutionOutcome,
    PhrasingOutcome,
    RunSummary,
    Spec,
    Status,
    Suite,
    ToolOutcome,
    TurnOutcome,
)
from highbar.domain.models import Test as Case
from highbar.domain.models import TestOutcome as Outcome
from highbar.reporters.json_report import JsonReport, ReportError

STARTED = "2026-10-30T11:49:00.120Z"
SPEC = Spec(agent=AgentConfig(endpoint="http://x/chat"), suites=[Suite(name="S", tests=[Case()])])


def _test(*, response: str | None = "coração", args: dict[str, Any] | None = None) -> Outcome:
    tool = ToolOutcome("lookup", args if args is not None else {"q": "x"}, None, 120.0)
    late = ToolOutcome("log", {}, "boom", 1.0, late=True)
    turn = TurnOutcome("olá", response, 5.5, {"test": "abc"}, (tool, late))
    passed = ExecutionOutcome("abc", 1, STARTED, Status.PASSED, 6.0, (), (turn,))
    broken = ExecutionOutcome(
        "def", 2, STARTED, Status.ERROR, 1.0, ("HTTP error",), (TurnOutcome("olá", None, None, {}),)
    )
    return Outcome(
        id="S/olá",
        suite="S",
        name="olá",
        definition_sha256="ab" * 32,
        status=Status.ERROR,
        runs=2,
        min_pass=1,
        counts=Counts(passed=2, failed=0, error=1),
        duration_ms=7.0,
        errors=("HTTP error (1 of 2 runs)",),
        phrasings=(
            PhrasingOutcome("trigger", "olá", Status.ERROR, Counts(1, 0, 1), (passed, broken)),
            PhrasingOutcome("variation", "oi", Status.PASSED, Counts(1, 0, 0), (passed,)),
            PhrasingOutcome("conversation", None, Status.PASSED, Counts(), ()),
        ),
    )


def _summary(*tests: Outcome) -> RunSummary:
    return RunSummary(
        total=len(tests),
        passed=0,
        failed=0,
        errored=len(tests),
        duration_ms=7.0,
        variations=1,
        tests=tests,
        warnings=("careful",),
    )


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory outside any repository, used as the cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _spec_file(directory: Path, name: str = "x.highbar.yaml") -> str:
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("suites: []\n", encoding="utf-8")
    return str(path)


def _finished(path: Path, spec_path: str, summary: RunSummary, *, code: int = 1) -> dict[str, Any]:
    report = JsonReport(path, version="1.2.3")
    report.start()
    report.plan([(spec_path, SPEC)])
    report.add(spec_path, SPEC, summary, True)
    report.finish(code)
    return _read(path)


class TestStart:
    def test_j4_start_replaces_an_older_report(self, workdir: Path) -> None:
        path = workdir / "report.json"
        path.write_text('{"exit_code": 0, "complete": true}', encoding="utf-8")

        JsonReport(path, version="1.2.3").start()

        data = _read(path)
        assert data["exit_code"] is None
        assert data["complete"] is False
        assert data["specs"] == []

    def test_parent_directories_are_created(self, workdir: Path) -> None:
        path = workdir / "a" / "b" / "report.json"

        JsonReport(path, version="1.2.3").start()

        assert path.exists()

    def test_j6_unwritable_path_raises_report_error(self, workdir: Path) -> None:
        locked = workdir / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            with pytest.raises(ReportError, match="locked"):
                JsonReport(locked / "report.json", version="1.2.3").start()
        finally:
            locked.chmod(0o700)

    def test_a_directory_as_target_raises_report_error(self, workdir: Path) -> None:
        target = workdir / "reports"
        target.mkdir()

        with pytest.raises(ReportError, match="reports"):
            JsonReport(target, version="1.2.3").start()

        assert list(workdir.iterdir()) == [target]


class TestWrite:
    def test_write_leaves_no_temporary_file(self, workdir: Path) -> None:
        _finished(workdir / "report.json", _spec_file(workdir), _summary(_test()))

        assert sorted(p.name for p in workdir.iterdir()) == ["report.json", "x.highbar.yaml"]

    def test_a_failed_rename_leaves_the_old_file_and_no_temporary(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = workdir / "report.json"
        report = JsonReport(path, version="1.2.3")
        report.start()
        before = path.read_bytes()

        def fail(*args: object) -> None:
            raise OSError("no space left")

        monkeypatch.setattr("highbar.reporters.json_report.os.replace", fail)

        with pytest.raises(ReportError, match="no space left"):
            report.finish(0)

        assert path.read_bytes() == before
        assert [p.name for p in workdir.iterdir()] == ["report.json"]

    def test_surrogate_in_a_response_is_still_a_valid_file(self, workdir: Path) -> None:
        path = workdir / "report.json"
        response = json.loads('"broken \\ud800 emoji"')

        data = _finished(path, _spec_file(workdir), _summary(_test(response=response)))

        turn = data["specs"][0]["tests"][0]["phrasings"][0]["executions"][0]["turns"][0]
        assert "broken" in turn["response"]

    def test_nan_in_args_is_null(self, workdir: Path) -> None:
        path = workdir / "report.json"
        args = {"score": math.nan, "top": [math.inf]}

        _finished(path, _spec_file(workdir), _summary(_test(args=args)))

        # json.loads would accept the bare NaN; a strict reader would not.
        def refuse(constant: str) -> None:
            raise AssertionError(f"{constant} in the file")

        data = json.loads(path.read_text(encoding="utf-8"), parse_constant=refuse)
        tool = data["specs"][0]["tests"][0]["phrasings"][0]["executions"][0]["turns"][0]["tools"][0]
        assert tool["args"] == {"score": None, "top": [None]}

    def test_text_stays_readable(self, workdir: Path) -> None:
        path = workdir / "report.json"

        _finished(path, _spec_file(workdir), _summary(_test()))

        assert "coração" in path.read_text(encoding="utf-8")

    def test_a_value_from_yaml_that_is_not_json_is_written_as_text(self, workdir: Path) -> None:
        path = workdir / "report.json"
        summary = _summary(_test(args={"when": Path("x")}))

        data = _finished(path, _spec_file(workdir), summary)

        assert "when" in json.dumps(data)

    def test_file_mode_follows_the_umask(self, workdir: Path) -> None:
        path = workdir / "report.json"
        previous = os.umask(0o027)
        try:
            JsonReport(path, version="1.2.3").start()
        finally:
            os.umask(previous)

        assert path.stat().st_mode & 0o777 == 0o640

    def test_existing_file_keeps_its_mode(self, workdir: Path) -> None:
        path = workdir / "report.json"
        path.write_text("{}", encoding="utf-8")
        path.chmod(0o600)

        JsonReport(path, version="1.2.3").start()

        assert path.stat().st_mode & 0o777 == 0o600


class TestBody:
    def test_report_matches_the_schema(self, workdir: Path) -> None:
        path = workdir / "report.json"
        data = _finished(path, _spec_file(workdir), _summary(_test()))

        schema_file = resources.files("highbar.reporters").joinpath("report.schema.json")
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator(schema).validate(data)

    def test_an_unfinished_report_matches_the_schema_too(self, workdir: Path) -> None:
        path = workdir / "report.json"
        JsonReport(path, version="1.2.3").start()

        schema_file = resources.files("highbar.reporters").joinpath("report.schema.json")
        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(_read(path))

    def test_the_body_says_what_ran(self, workdir: Path) -> None:
        spec_path = _spec_file(workdir)

        data = _finished(workdir / "report.json", spec_path, _summary(_test()), code=1)

        assert data["schema_version"] == 1
        assert data["highbar_version"] == "1.2.3"
        assert data["exit_code"] == 1
        assert data["complete"] is True
        assert data["stopped"] is None
        assert data["summary"] == {
            "total": 1,
            "passed": 0,
            "failed": 0,
            "errored": 1,
            "variations": 1,
        }
        (spec,) = data["specs"]
        assert spec["path"] == spec_path
        assert spec["complete"] is True
        assert spec["warnings"] == ["careful"]
        (test,) = spec["tests"]
        assert test["status"] == "error"
        assert test["counts"] == {"passed": 2, "failed": 0, "error": 1}

    def test_sha256_is_the_file_as_it_was_planned(self, workdir: Path) -> None:
        spec_path = _spec_file(workdir)
        report = JsonReport(workdir / "report.json", version="1.2.3")
        report.start()
        report.plan([(spec_path, SPEC)])
        expected = __import__("hashlib").sha256(Path(spec_path).read_bytes()).hexdigest()

        Path(spec_path).write_text("changed during the run\n", encoding="utf-8")
        report.add(spec_path, SPEC, _summary(), True)
        report.finish(0)

        assert _read(workdir / "report.json")["specs"][0]["sha256"] == expected

    def test_complete_needs_every_planned_spec(self, workdir: Path) -> None:
        first, second = _spec_file(workdir, "a.highbar.yaml"), _spec_file(workdir, "b.highbar.yaml")
        report = JsonReport(workdir / "report.json", version="1.2.3")
        report.start()
        report.plan([(first, SPEC), (second, SPEC)])
        report.add(first, SPEC, _summary(), True)
        report.finish(0)

        assert _read(workdir / "report.json")["complete"] is False

    def test_a_spec_that_stopped_is_not_complete(self, workdir: Path) -> None:
        spec_path = _spec_file(workdir)
        report = JsonReport(workdir / "report.json", version="1.2.3")
        report.start()
        report.plan([(spec_path, SPEC)])
        report.add(spec_path, SPEC, _summary(), False)
        report.stop("interrupted", "stopped by signal 15")
        report.finish(143)

        data = _read(workdir / "report.json")
        assert data["complete"] is False
        assert data["specs"][0]["complete"] is False
        assert data["stopped"] == {"reason": "interrupted", "message": "stopped by signal 15"}
        assert data["exit_code"] == 143

    def test_stopped_message_is_masked(self, workdir: Path) -> None:
        agent = AgentConfig(endpoint="http://x/chat", secrets={"s3cr3t-value": "TOKEN"})
        spec = Spec(agent=agent, suites=[])
        spec_path = _spec_file(workdir)
        report = JsonReport(workdir / "report.json", version="1.2.3")
        report.start()
        report.plan([(spec_path, spec)])
        report.stop("crashed", "ValueError: bad s3cr3t-value")
        report.finish(1)

        assert _read(workdir / "report.json")["stopped"]["message"] == "ValueError: bad ${TOKEN}"


class TestGit:
    @staticmethod
    def _repository(root: Path) -> None:
        def git(*args: str) -> None:
            subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

        root.mkdir(parents=True, exist_ok=True)
        git("init", "-q")
        (root / "f").write_text("x", encoding="utf-8")
        git("add", "f")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")

    def test_j14_key_is_relative_to_the_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._repository(tmp_path)
        _spec_file(tmp_path / "evals" / "specs")
        monkeypatch.chdir(tmp_path / "evals")

        data = _finished(tmp_path / "r.json", "specs/x.highbar.yaml", _summary())

        (spec,) = data["specs"]
        assert spec["path"] == "specs/x.highbar.yaml"
        assert spec["key"] == "evals/specs/x.highbar.yaml"
        assert len(data["git_sha"]) == 40

    def test_key_is_the_path_outside_a_repository(self, workdir: Path) -> None:
        _spec_file(workdir)

        data = _finished(workdir / "r.json", "x.highbar.yaml", _summary())

        assert data["specs"][0]["key"] == "x.highbar.yaml"

    def test_git_sha_is_null_outside_a_repository(self, workdir: Path) -> None:
        data = _finished(workdir / "r.json", _spec_file(workdir), _summary())

        assert data["git_sha"] is None

    def test_git_that_is_not_installed_is_null(
        self, workdir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(workdir))

        data = _finished(workdir / "r.json", _spec_file(workdir), _summary())

        assert data["git_sha"] is None
