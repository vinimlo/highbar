"""HB-24: a test that starts in the middle of a conversation.

The earlier turns are declared in the test, not run live: each live turn spends a call
to the model and varies from run to run, which changes the context of the turn that
matters. highbar does not read the history; it hands it to the session hook, and the
agent's own adapter seeds it before the turn that is checked.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from highbar.domain.models import PythonHook
from highbar.domain.models import Test as Case
from highbar.engine.parser import parse_spec
from highbar.engine.placeholders import SETUP, Values, check_placeholders, render
from highbar.engine.schema import SpecError
from highbar.engine.session import SessionContext
from tests.unit.engine.test_parser_agent import MANAGED, _spec
from tests.unit.engine.test_session import SAYS_OK, _command, _FakeSession, _hooks, _run

WITH_SESSION = MANAGED + 'session:\n  setup: "open {{test.history}}"\n'
HISTORY = (
    "        history:\n"
    "          - {role: user, content: 'A lemonade, please'}\n"
    "          - {role: assistant, content: 'One lemonade coming.'}\n"
)


# An agent with no state of its own: the conversation goes in every request (HB-24 v2).
REQUEST_WITH_HISTORY = 'request:\n  body: {message: "{{message}}", messages: "{{test.history}}"}\n'


def _suites(*tests: str) -> str:
    return "suites:\n  - name: S\n    tests:\n" + "".join(tests)


def _test(trigger: str, extra: str = "") -> str:
    return (
        f'      - trigger: "{trigger}"\n        expect: {{response: {{contains: "ok"}}}}\n' + extra
    )


class TestTheSpec:
    def test_hb24_a_test_declares_its_history(self, tmp_path: Path) -> None:
        spec = parse_spec(_spec(tmp_path, WITH_SESSION, _suites(_test("another one", HISTORY))))

        assert spec.suites[0].tests[0].history == [
            {"role": "user", "content": "A lemonade, please"},
            {"role": "assistant", "content": "One lemonade coming."},
        ]

    def test_hb24_a_history_needs_a_session_setup_to_seed_it(self, tmp_path: Path) -> None:
        """Without one, the test would start from an empty conversation and say it did not."""
        with pytest.raises(
            SpecError, match=r"suites\[0\]\.tests\[0\]\.history: needs agent\.session with a setup"
        ):
            parse_spec(_spec(tmp_path, MANAGED, _suites(_test("another one", HISTORY))))

    def test_hb24_a_teardown_alone_does_not_seed_it(self, tmp_path: Path) -> None:
        agent = MANAGED + 'session:\n  teardown: "close"\n'

        with pytest.raises(SpecError, match=r"needs agent\.session with a setup"):
            parse_spec(_spec(tmp_path, agent, _suites(_test("another one", HISTORY))))

    def test_hb24_a_command_setup_must_hand_it_on(self, tmp_path: Path) -> None:
        """A command sees only its placeholders: without {{test.history}} it seeds nothing."""
        agent = MANAGED + 'session:\n  setup: "open"\n'

        with pytest.raises(
            SpecError,
            match=r"suites\[0\]\.tests\[0\]\.history: agent\.session\.setup does not use "
            r"'\{\{test\.history\}\}'",
        ):
            parse_spec(_spec(tmp_path, agent, _suites(_test("another one", HISTORY))))

    def test_hb24_an_http_setup_must_hand_it_on(self, tmp_path: Path) -> None:
        url = '"http://localhost:9/open"'
        bare = MANAGED + f"session:\n  setup:\n    http: {{url: {url}}}\n"
        seeded = (
            MANAGED
            + f'session:\n  setup:\n    http: {{url: {url}, body: {{m: "{{{{test.history}}}}"}}}}\n'
        )
        suites = _suites(_test("another one", HISTORY))

        with pytest.raises(SpecError, match=r"agent\.session\.setup does not use"):
            parse_spec(_spec(tmp_path, bare, suites))
        (test,) = parse_spec(_spec(tmp_path, seeded, suites)).suites[0].tests
        assert len(test.history) == 2

    def test_hb24_a_python_setup_reads_it_from_ctx(self, tmp_path: Path) -> None:
        agent = MANAGED + 'session:\n  setup: {python: "hooks:open_session"}\n'

        (test,) = (
            parse_spec(_spec(tmp_path, agent, _suites(_test("another one", HISTORY))))
            .suites[0]
            .tests
        )
        assert len(test.history) == 2

    def test_hb24_a_trigger_does_not_read_it(self, tmp_path: Path) -> None:
        """A trigger goes as written: the history there would be sent as braces.

        The message points to the two places a history goes: the setup, which seeds it,
        and agent.request, in a single-turn test (HB-24 v2, review of #53).
        """
        suites = _suites(_test("as before: {{test.history}}", HISTORY))

        with pytest.raises(SpecError) as caught:
            parse_spec(_spec(tmp_path, WITH_SESSION, suites))

        assert str(caught.value).endswith(
            "suites[0].tests[0].trigger: '{{test.history}}' is not read in a trigger: the "
            "session setup seeds it (ctx.history, or {{test.history}} in a command or HTTP "
            "setup), or agent.request hands it on in a single-turn test"
        )

    @pytest.mark.parametrize(
        "session",
        ["", 'session:\n  setup: "open"\n'],
        ids=["no-setup", "a-setup-that-ignores-it"],
    )
    def test_hb24_a_request_that_sends_it_hands_it_to_a_stateless_agent(
        self, tmp_path: Path, session: str
    ) -> None:
        """v2: an agent with no state of its own takes the conversation in each request.

        In a single-turn test that request is the only one, so it carries the whole
        conversation before the message: it hands the history on, as a setup would."""
        agent = MANAGED + REQUEST_WITH_HISTORY + session
        spec = parse_spec(_spec(tmp_path, agent, _suites(_test("another one", HISTORY))))

        (test,) = spec.suites[0].tests

        assert len(test.history) == 2

    def test_hb24_a_conversation_of_one_turn_is_a_single_turn_test(self, tmp_path: Path) -> None:
        suites = _suites(
            "      - turns:\n"
            '          - trigger: "another one"\n'
            '            expect: {response: {contains: "ok"}}\n' + HISTORY
        )

        spec = parse_spec(_spec(tmp_path, MANAGED + REQUEST_WITH_HISTORY, suites))

        assert len(spec.suites[0].tests[0].history) == 2

    def test_hb24_a_conversation_does_not_send_it_in_the_request(self, tmp_path: Path) -> None:
        """From its second turn on, the request would carry the history without the turns
        the test itself ran: the agent would answer a conversation that did not happen."""
        suites = _suites(
            "      - turns:\n"
            '          - trigger: "another one"\n'
            '            expect: {response: {contains: "ok"}}\n'
            '          - trigger: "and a cookie"\n'
            '            expect: {response: {contains: "ok"}}\n' + HISTORY
        )

        with pytest.raises(SpecError) as caught:
            parse_spec(_spec(tmp_path, MANAGED + REQUEST_WITH_HISTORY, suites))

        assert str(caught.value).endswith(
            "suites[0].tests[0].history: '{{test.history}}' in agent.request hands it on only "
            "in a single-turn test: from the second turn on, the request would carry it "
            "without the turns this test ran. Seed it in agent.session's setup instead"
        )

    @pytest.mark.parametrize(
        ("history", "message"),
        [
            ("{role: user}", r"history: must be a list"),
            ("[]", r"history: is empty"),
            ("[{role: user}]", r"history\[0\]: needs 'content'"),
            ("[{role: tool, content: x}]", r"history\[0\]\.role: must be user or assistant"),
            ("[{role: user, content: 3}]", r"history\[0\]\.content: must be text"),
            ("[{role: user, content: ''}]", r"history\[0\]\.content: cannot be empty"),
            ("[{role: user, content: x, name: y}]", r"history\[0\]: unknown key 'name'"),
        ],
    )
    def test_hb24_each_message_is_a_role_and_its_text(
        self, tmp_path: Path, history: str, message: str
    ) -> None:
        suites = _suites(_test("another one", f"        history: {history}\n"))

        with pytest.raises(SpecError, match=rf"suites\[0\]\.tests\[0\]\.{message}"):
            parse_spec(_spec(tmp_path, WITH_SESSION, suites))

    def test_hb24_a_conversation_can_start_from_a_history(self, tmp_path: Path) -> None:
        suites = _suites(
            "      - turns:\n"
            '          - trigger: "another one"\n'
            '            expect: {response: {contains: "ok"}}\n' + HISTORY
        )

        (test,) = parse_spec(_spec(tmp_path, WITH_SESSION, suites)).suites[0].tests
        assert len(test.history) == 2

    def test_hb24_text_in_a_history_is_kept_as_written(self, tmp_path: Path) -> None:
        """No ${VAR} under a test, as for args, and no placeholder is read in it."""
        history = "        history: [{role: user, content: 'cost ${PRICE} for {{message}}'}]\n"

        (test,) = (
            parse_spec(_spec(tmp_path, WITH_SESSION, _suites(_test("another one", history))))
            .suites[0]
            .tests
        )
        assert test.history == [{"role": "user", "content": "cost ${PRICE} for {{message}}"}]

    def test_hb24_a_history_changes_what_the_test_measures(self) -> None:
        plain = Case(trigger="a", expect=SAYS_OK)
        seeded = Case(trigger="a", expect=SAYS_OK, history=[{"role": "user", "content": "hi"}])

        assert Case(trigger="a", expect=SAYS_OK, history=[]).definition_sha256 == (
            plain.definition_sha256
        )
        assert seeded.definition_sha256 != plain.definition_sha256


class TestTheHook:
    def test_hb24_test_history_goes_whole_as_json(self) -> None:
        history = [{"role": "user", "content": "say {{message}}"}]
        values = Values(message="m", test_id="t", test_name="n", session={}, history=history)

        assert render({"h": "{{test.history}}", "t": "seed {{test.history}}"}, values) == {
            "h": history,
            "t": 'seed [{"role": "user", "content": "say {{message}}"}]',
        }

    def test_hb24_the_setup_hook_can_use_it(self) -> None:
        check_placeholders(["seed", "{{test.history}}"], "setup", allowed=SETUP, has_session=True)

    def test_hb24_a_command_hook_gets_it_as_one_argument(self) -> None:
        hooks = _hooks(
            _command(
                "import json, sys; print(json.dumps({'n': len(json.loads(sys.argv[1]))}))",
                "{{test.history}}",
            )
        )
        ctx = SessionContext(
            test_id="a",
            test_name="n",
            spec_path="s",
            history=[{"role": "user", "content": "a; b"}, {"role": "assistant", "content": "c"}],
        )

        assert hooks.setup(ctx) == {"n": 2}

    def test_hb24_a_python_hook_reads_ctx_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delitem(sys.modules, "hb24_hooks", raising=False)
        (tmp_path / "hb24_hooks.py").write_text(
            textwrap.dedent("""\
                def open_session(ctx):
                    return {"last": ctx.history[-1]["content"]}
                """),
            encoding="utf-8",
        )
        hooks = _hooks(PythonHook("hb24_hooks:open_session"))
        ctx = SessionContext(
            test_id="a", test_name="n", spec_path="s", history=[{"role": "user", "content": "hi"}]
        )

        assert hooks.setup(ctx) == {"last": "hi"}


class TestTheRun:
    def test_hb24_each_execution_gets_its_tests_history(self) -> None:
        session = _FakeSession()
        history = [{"role": "user", "content": "A lemonade, please"}]
        tests = [
            Case(trigger="another one", expect=SAYS_OK, history=history),
            Case(trigger="hi", expect=SAYS_OK),
        ]

        agent, _ = _run(tests, session)

        assert [list(ctx.history) for ctx in session.setups] == [history, []]
        assert [list(values.history) for values in agent.values] == [history, []]
