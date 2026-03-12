"""Tests for HTTP client adapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from gauntlet.adapters.http_client import HTTPClient


class TestHTTPClient:
    def test_send_posts_json_body(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"response": "hello"}
        mock_response.raise_for_status = MagicMock()

        with patch("gauntlet.adapters.http_client.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_cls.return_value = mock_client

            client = HTTPClient(endpoint="http://localhost:8000/chat")
            client.send("hello")

            mock_client.post.assert_called_once_with(
                "http://localhost:8000/chat",
                json={"message": "hello"},
            )

    def test_send_extracts_response_key(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"response": "world"}
        mock_response.raise_for_status = MagicMock()

        with patch("gauntlet.adapters.http_client.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_cls.return_value = mock_client

            client = HTTPClient(endpoint="http://localhost:8000/chat")
            result = client.send("hi")

        assert result == "world"

    def test_send_tries_message_key(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = {"message": "from message key"}
        mock_response.raise_for_status = MagicMock()

        with patch("gauntlet.adapters.http_client.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_cls.return_value = mock_client

            client = HTTPClient(endpoint="http://localhost:8000/chat")
            result = client.send("hi")

        assert result == "from message key"

    def test_send_stringifies_non_dict(self) -> None:
        mock_response = MagicMock()
        mock_response.json.return_value = "plain string"
        mock_response.raise_for_status = MagicMock()

        with patch("gauntlet.adapters.http_client.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_cls.return_value = mock_client

            client = HTTPClient(endpoint="http://localhost:8000/chat")
            result = client.send("hi")

        assert result == "plain string"

    def test_send_raises_on_http_error(self) -> None:
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server Error",
            request=MagicMock(),
            response=MagicMock(status_code=500),
        )

        with patch("gauntlet.adapters.http_client.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.post.return_value = mock_response
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_cls.return_value = mock_client

            client = HTTPClient(endpoint="http://localhost:8000/chat")
            with pytest.raises(httpx.HTTPStatusError):
                client.send("hi")
