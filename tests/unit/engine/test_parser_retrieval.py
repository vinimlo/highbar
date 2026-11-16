"""HB-3: expect.retrieval in the spec."""

from __future__ import annotations

from pathlib import Path

import pytest

from highbar.domain.models import ItemRef, RetrievalExpect
from highbar.engine.parser import parse_spec
from highbar.engine.schema import SpecError


def _spec(tmp_path: Path, retrieval: str) -> str:
    path = tmp_path / "spec.highbar.yaml"
    path.write_text(
        'agent:\n  endpoint: "http://localhost:8000/chat"\n  traces: t.jsonl\n'
        'suites:\n  - name: S\n    tests:\n      - trigger: "hi"\n'
        f"        expect:\n          retrieval: {retrieval}\n",
        encoding="utf-8",
    )
    return str(path)


def test_hb3_expect_retrieval_is_parsed(tmp_path: Path) -> None:
    spec = parse_spec(
        _spec(
            tmp_path,
            "{includes: [tr-7, {id: tr-2, source: sticky}], excludes: [Draft], "
            "max_items: 3, max_tokens: 2000, chunks_in_order: true}",
        )
    )

    expect = spec.suites[0].tests[0].expect
    assert expect.retrieval == RetrievalExpect(
        includes=(ItemRef("tr-7"), ItemRef("tr-2", "sticky")),
        excludes=("Draft",),
        max_items=3,
        max_tokens=2000,
        chunks_in_order=True,
    )
    assert expect.requires_observation


@pytest.mark.parametrize(
    ("retrieval", "message"),
    [
        ("{include: [x]}", r"unknown key 'include'.*did you mean 'includes'"),
        ("{includes: x}", r"includes must be a list"),
        (
            "{includes: [{id: x, source: cached}]}",
            r"source must be one of expanded, retrieved, sticky",
        ),
        ("{includes: [{source: sticky}]}", r"needs 'id' or 'title'"),
        ("{max_items: -1}", r"max_items must be a whole number of at least 0"),
        ("{chunks_in_order: false}", r"chunks_in_order can only be true"),
        ("{}", r"retrieval checks nothing"),
    ],
)
def test_hb3_a_wrong_retrieval_check_does_not_load(
    tmp_path: Path, retrieval: str, message: str
) -> None:
    with pytest.raises(SpecError, match=message):
        parse_spec(_spec(tmp_path, retrieval))
