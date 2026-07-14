"""Tests for HTTP client adapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from gauntlet.adapters.http_client import HTTPClient


@pytest.fixture
def mock_httpx():
    """Set up httpx mock with context manager support."""
    with patch("gauntlet.adapters.http_client.httpx.Client") as mock_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_cls.return_value = mock_client

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_client.post.return_value = mock_response

        yield mock_client, mock_response


class TestHTTPClient:
    def test_send_posts_json_body(self, mock_httpx: tuple) -> None:
        mock_client, mock_response = mock_httpx
        mock_response.json.return_value = {"response": "hello"}

        client = HTTPClient(endpoint="http://localhost:8000/chat")
        client.send("hello")

        mock_client.post.assert_called_once_with(
            "http://localhost:8000/chat",
            json={"message": "hello"},
        )

    def test_send_extracts_response_key(self, mock_httpx: tuple) -> None:
        mock_client, mock_response = mock_httpx
        mock_response.json.return_value = {"response": "world"}

        client = HTTPClient(endpoint="http://localhost:8000/chat")
        result = client.send("hi")

        assert result == "world"

    def test_send_tries_message_key(self, mock_httpx: tuple) -> None:
        mock_client, mock_response = mock_httpx
        mock_response.json.return_value = {"message": "from message key"}

        client = HTTPClient(endpoint="http://localhost:8000/chat")
        result = client.send("hi")

        assert result == "from message key"

    def test_send_stringifies_non_dict(self, mock_httpx: tuple) -> None:
        mock_client, mock_response = mock_httpx
        mock_response.json.return_value = "plain string"

        client = HTTPClient(endpoint="http://localhost:8000/chat")
        result = client.send("hi")

        assert result == "plain string"

    def test_send_raises_on_http_error(self, mock_httpx: tuple) -> None:
        mock_client, mock_response = mock_httpx
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server Error",
            request=MagicMock(),
            response=MagicMock(status_code=500),
        )

        client = HTTPClient(endpoint="http://localhost:8000/chat")
        with pytest.raises(httpx.HTTPStatusError):
            client.send("hi")
