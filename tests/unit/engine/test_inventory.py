"""The tools an agent declares it records (spec D4, after the rc1 onboarding rehearsal).

A guardrail on a tool highbar cannot see always passed: a tool without @hb.tool (FG1),
or a name typed wrong (FG2). With the agent's inventory, such a name is unobservable,
and the test is an error, not a pass.
"""

from __future__ import annotations

from highbar.domain.models import Expect, FunctionCallExpect, Status, Trace, TurnRecord, Window
from highbar.domain.models import Test as Case
from tests.unit.engine.fakes import FakeAgent
from tests.unit.engine.test_observation import _run


def _turn(test_id: str) -> TurnRecord:
    return TurnRecord(ts=1.0, context={"test": test_id})


def _calls(*names: str):  # type: ignore[no-untyped-def]
    def script(message: str, test_id: str) -> Window:
        tools = [
            Trace(
                fn=n, args={}, result=None, started_at=1.0, ended_at=1.1, context={"test": test_id}
            )
            for n in names
        ]
        return Window(tools=tools, turns=[_turn(test_id)])

    return script


BANK = ("get_balance", "transfer", "close_account")


class TestUnobservableNames:
    def test_fg1_forbid_on_a_tool_the_agent_does_not_record_is_an_error(self) -> None:
        agent = FakeAgent(_calls(), declares=("get_balance", "transfer"))

        results, _, _ = _run(
            [Case(trigger="close it", expect=Expect(forbid=("close_account",)))], agent
        )

        ((_, result),) = results
        assert result.status is Status.ERROR
        assert "cannot see calls to 'close_account'" in result.errors[0]
        assert "get_balance, transfer" in result.errors[0]

    def test_fg2_a_typo_in_forbid_is_an_error_that_names_the_closest_tool(self) -> None:
        agent = FakeAgent(_calls(), declares=BANK)

        results, _, _ = _run(
            [Case(trigger="close it", expect=Expect(forbid=("close_acount",)))], agent
        )

        ((_, result),) = results
        assert result.status is Status.ERROR
        assert "did you mean 'close_account'?" in result.errors[0]

    def test_a_declared_tool_that_is_not_called_passes_the_guardrail(self) -> None:
        agent = FakeAgent(_calls("get_balance"), declares=BANK)

        results, _, _ = _run(
            [Case(trigger="close it", expect=Expect(forbid=("close_account",)))], agent
        )

        assert results[0][1].status is Status.PASSED

    def test_a_declared_tool_that_is_called_still_fails_the_guardrail(self) -> None:
        agent = FakeAgent(_calls("close_account"), declares=BANK)

        results, _, _ = _run(
            [Case(trigger="close it", expect=Expect(forbid=("close_account",)))], agent
        )

        assert results[0][1].status is Status.FAILED

    def test_a_tool_seen_in_an_earlier_record_counts_as_recorded(self) -> None:
        """A funnel tool written by hb.record() and never declared: having seen it is enough."""

        def script(message: str, test_id: str) -> Window:
            return (
                _calls("search")(message, test_id)
                if message == "find"
                else _calls()(message, test_id)
            )

        agent = FakeAgent(script, declares=("get_balance",))

        results, _, _ = _run(
            [
                Case(
                    trigger="find",
                    expect=Expect(function_calls=[FunctionCallExpect(name="search")]),
                ),
                Case(trigger="other", expect=Expect(forbid=("search",))),
            ],
            agent,
        )

        assert [r.status for _, r in results] == [Status.PASSED, Status.PASSED]

    def test_an_expected_call_is_unobservable_only_when_none_of_its_names_is_recorded(
        self,
    ) -> None:
        agent = FakeAgent(_calls("get_balance"), declares=BANK)

        results, _, _ = _run(
            [
                Case(
                    trigger="a",
                    expect=Expect(
                        function_calls=[FunctionCallExpect(name=["get_balance", "balance"])]
                    ),
                ),
                Case(
                    trigger="b",
                    expect=Expect(function_calls=[FunctionCallExpect(name=["send_money", "pay"])]),
                ),
            ],
            agent,
        )

        assert results[0][1].status is Status.PASSED
        assert results[1][1].status is Status.ERROR
        assert "cannot see calls to 'send_money' or 'pay'" in results[1][1].errors[0]


class TestWithoutDeclarations:
    def test_forbid_warns_once_and_is_judged_as_before(self) -> None:
        agent = FakeAgent(_calls())  # an older SDK, or a writer without `tools`

        results, _, reporter = _run(
            [
                Case(trigger="a", expect=Expect(forbid=("close_account",))),
                Case(trigger="b", expect=Expect(forbid=("close_acount",))),
            ],
            agent,
        )

        assert [r.status for _, r in results] == [Status.PASSED, Status.PASSED]
        warnings = [c[0][0] for c in reporter.on_warning.call_args_list]
        (warning,) = [w for w in warnings if "has not declared" in w]
        # PR 12 review I2: every unseen name, the later test's too.
        assert "close_account" in warning and "close_acount" in warning

    def test_a_spec_without_forbid_gets_no_warning(self) -> None:
        agent = FakeAgent(_calls("get_balance"))

        _, _, reporter = _run(
            [
                Case(
                    trigger="a",
                    expect=Expect(function_calls=[FunctionCallExpect(name="get_balance")]),
                )
            ],
            agent,
        )

        reporter.on_warning.assert_not_called()


class TestReviewFindings:
    """PR 12 review: the check never hides a failure (I1), and its hint is honest (M3)."""

    def test_i1_an_unseen_name_does_not_hide_a_forbidden_call(self) -> None:
        agent = FakeAgent(_calls("transfer"), declares=BANK)

        results, _, _ = _run(
            [Case(trigger="send", expect=Expect(forbid=("transfer", "close_acount")))], agent
        )

        ((_, result),) = results
        assert result.status is Status.FAILED
        assert any("forbidden tool 'transfer' was called" in e for e in result.errors)
        assert any("cannot see calls to 'close_acount'" in e for e in result.errors)

    def test_i1_an_unseen_name_does_not_drop_an_earlier_tests_late_call(self) -> None:
        def script(message: str, test_id: str):  # type: ignore[no-untyped-def]
            now = Window(turns=[_turn(test_id)])
            if message == "first":
                late = Trace(
                    fn="close_account",
                    args={},
                    result=None,
                    started_at=2.0,
                    ended_at=2.1,
                    context={"test": test_id},
                )
                return now, Window(tools=[late])
            return now

        _, _, reporter = _run(
            [
                Case(trigger="first", expect=Expect(forbid=("close_account",))),
                Case(trigger="second", expect=Expect(forbid=("close_acount",))),
            ],
            FakeAgent(script, declares=BANK),
        )

        summary = reporter.on_run_end.call_args[0][0]
        assert (summary.passed, summary.failed, summary.errored) == (0, 1, 1)

    def test_m3_the_hint_says_to_remove_a_tool_the_agent_does_not_have(self) -> None:
        agent = FakeAgent(_calls(), declares=BANK)

        results, _, _ = _run([Case(trigger="a", expect=Expect(forbid=("wire_abroad",)))], agent)

        assert "remove it from the spec" in results[0][1].errors[0]


class TestAttachedFileHistory:
    """PR 12 review C1: a long-lived file holds an older agent's declarations."""

    def test_c1_a_tool_an_earlier_agent_declared_is_not_known(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        import json

        from highbar.adapters.file_collector import FileTraceCollector

        path = tmp_path / "traces.jsonl"
        history = [
            {"v": 1, "type": "hello", "ts": 1, "sdk": "x/1", "pid": 1},
            {"v": 1, "type": "tools", "ts": 1, "names": ["close_account", "get_balance"]},
            {"v": 1, "type": "tool", "ts": 2, "fn": "close_account", "args": {}},
            {"v": 1, "type": "hello", "ts": 3, "sdk": "x/2", "pid": 2},
            {"v": 1, "type": "tools", "ts": 3, "names": ["get_balance"]},
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in history), encoding="utf-8")
        collector = FileTraceCollector(path)

        class Agent:
            """Version 2 closes the account without recording it: only its turn is written."""

            path = collector.path
            checkpoint = collector.checkpoint
            collect = collector.collect
            declared = collector.declared

            def send(self, message: str, *, values) -> str:  # type: ignore[no-untyped-def]
                turn = {"v": 1, "type": "turn", "ts": 4, "context": {"test": values.test_id}}
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(turn) + "\n")
                return "Your account is closed."

        results, _, _ = _run(
            [Case(trigger="close it", expect=Expect(forbid=("close_account",)))],
            Agent(),  # type: ignore[arg-type]
            managed=False,
        )

        ((_, result),) = results
        assert result.status is Status.ERROR
        assert "cannot see calls to 'close_account'" in result.errors[0]
