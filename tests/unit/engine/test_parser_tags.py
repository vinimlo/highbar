"""HB-17 v1: ``tags:`` in a suite and in a test, labels for ``--tag``."""

from __future__ import annotations

from pathlib import Path

import pytest

from highbar.domain.models import Expect, ResponseExpect
from highbar.domain.models import Test as Case
from highbar.engine.parser import parse_spec
from highbar.engine.schema import SpecError
from tests.unit.engine.test_parser_agent import MANAGED, _spec

OK = Expect(response=ResponseExpect(matcher={"contains": "ok"}))


def _suites(suite_extra: str = "", test_extra: str = "") -> str:
    return (
        "suites:\n  - name: S\n" + suite_extra + "    tests:\n"
        '      - trigger: "hi"\n        expect: {response: {contains: "ok"}}\n'
        + test_extra
        + '      - trigger: "bye"\n        expect: {response: {contains: "ok"}}\n'
    )


class TestTags:
    def test_hb17_a_test_has_its_suites_tags_and_its_own(self, tmp_path: Path) -> None:
        suites = _suites("    tags: [orders]\n", "        tags: [smoke, orders]\n")

        first, second = parse_spec(_spec(tmp_path, MANAGED, suites)).suites[0].tests

        assert first.tags == ("orders", "smoke")
        assert second.tags == ("orders",)

    @pytest.mark.parametrize(
        ("tags", "message"),
        [
            ("smoke", r"tags must be a list of names"),
            ("[]", r"tags must be a list of names"),
            ("[smoke, '']", r"tags\[1\] must be a name, got ''"),
            ("[smoke, 3]", r"tags\[1\] must be a name, got 3"),
        ],
    )
    def test_hb17_tags_are_names(self, tmp_path: Path, tags: str, message: str) -> None:
        with pytest.raises(SpecError, match=rf"suites\[0\]\.tests\[0\]: {message}"):
            parse_spec(_spec(tmp_path, MANAGED, _suites("", f"        tags: {tags}\n")))

    def test_hb17_tags_are_labels_out_of_the_hash(self) -> None:
        """Like name and id: they change neither the message nor what counts as right."""
        plain = Case(trigger="a", expect=OK)
        tagged = Case(trigger="a", expect=OK, tags=("smoke",))

        assert tagged.definition_sha256 == plain.definition_sha256


@pytest.mark.parametrize("tag", ["' smoke'", "'smoke '", '"\\tsmoke"'])
def test_hb17_a_tag_with_spaces_around_it_is_refused(tmp_path: Path, tag: str) -> None:
    """--tag smoke would never pick ' smoke': the typo is said at load (the review of #46)."""
    with pytest.raises(
        SpecError,
        match=r"suites\[0\]\.tests\[0\]: tags\[1\] must be a name without spaces around it",
    ):
        parse_spec(_spec(tmp_path, MANAGED, _suites("", f"        tags: [smoke, {tag}]\n")))
