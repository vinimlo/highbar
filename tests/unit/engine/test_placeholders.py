"""{{message}}, {{test.*}} and {{session.*}} in the request, correlate and session hooks."""

from __future__ import annotations

import pytest

from highbar.engine.placeholders import (
    ALL,
    PlaceholderError,
    Values,
    check_placeholders,
    render,
    uses_placeholder,
)
from highbar.engine.schema import SpecError

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


@pytest.mark.parametrize("value", ["{{session.token}}", "Bearer {{session.token}}"])
def test_hb20_a_null_session_value_is_an_error_here_too(value: str) -> None:
    """Review of #36: as in a trigger, a null most likely means the setup gave no value."""
    values = Values(message="m", test_id="t", test_name="n", session={"token": None})

    with pytest.raises(
        PlaceholderError, match=r"^\{\{session\.token\}\}: the value is null: the session setup"
    ):
        render({"auth": value}, values)


@pytest.mark.parametrize("value", ["{{session}}", "{{ session }}"])
def test_hb20_session_with_no_path_does_not_load_here_either(value: str) -> None:
    """It loaded, and only failed at run time as `{{session.session}}: no key 'session'`."""
    with pytest.raises(
        SpecError, match=r"^agent\.request\.body\.x: '\{\{session\}\}' names no value"
    ):
        check_placeholders({"x": value}, "agent.request.body", allowed=ALL, has_session=True)
