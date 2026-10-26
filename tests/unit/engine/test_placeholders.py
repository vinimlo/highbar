"""{{message}}, {{test.*}} and {{session.*}} in the request, correlate and session hooks."""

from __future__ import annotations

import pytest

from highbar.engine.placeholders import PlaceholderError, Values, render, uses_placeholder

VALUES = Values(
    message="cancel order 7",
    test_id="abc123",
    test_name="cancel",
    session={"cid": 42, "user": {"name": "Ana"}, "items": ["a", "b"], "headers": {}},
)


def test_whole_placeholder_keeps_the_json_type() -> None:
    assert render({"id": "{{session.cid}}"}, VALUES) == {"id": 42}
    assert render("{{ session.user }}", VALUES) == {"name": "Ana"}


def test_embedded_placeholder_is_interpolated_as_text() -> None:
    assert render("conv-{{session.cid}}", VALUES) == "conv-42"
    assert render("items {{session.items}}", VALUES) == 'items ["a", "b"]'


def test_message_and_test_values() -> None:
    body = {"message": "{{message}}", "meta": ["{{test.id}}", "for {{test.name}}"]}

    assert render(body, VALUES) == {
        "message": "cancel order 7",
        "meta": ["abc123", "for cancel"],
    }


def test_nested_and_indexed_session_paths() -> None:
    assert render("{{session.user.name}}/{{session.items.1}}", VALUES) == "Ana/b"


def test_missing_session_path_names_the_available_keys() -> None:
    with pytest.raises(PlaceholderError, match=r"\{\{session\.conv\}\}: no key 'conv' \(keys: cid"):
        render("{{session.conv}}", VALUES)


def test_rendered_values_are_not_rendered_again() -> None:
    values = Values(message="{{test.id}}", test_id="abc", test_name="t", session={})

    assert render("{{message}}", values) == "{{test.id}}"


def test_non_text_values_pass_through() -> None:
    assert render({"n": 3, "ok": True, "none": None}, VALUES) == {"n": 3, "ok": True, "none": None}


def test_uses_placeholder() -> None:
    assert uses_placeholder({"a": ["x {{ message }}"]}, "message")
    assert not uses_placeholder({"a": "{{test.id}}"}, "message")
