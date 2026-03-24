"""Tests for the message router."""
import pytest
import os
import httpx

# Minimal env so config imports don't crash
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:AAtesttoken")
os.environ.setdefault("ALLOWED_USER_ID", "12345678")
os.environ.setdefault("CLI_RUNNER", "generic")
os.environ.setdefault("CLI_COMMAND", "echo")

from router import route_message
from unittest.mock import MagicMock, patch

@pytest.fixture
def mock_httpx_post():
    with patch("httpx.AsyncClient.post") as mock_post:
        yield mock_post

@pytest.fixture
def sample_instances():
    return [
        {"id": 1, "title": "First Instance"},
        {"id": 2, "title": "Second Instance"},
        {"id": 3, "title": "Third Instance"},
    ]


@pytest.mark.asyncio
async def test_route_message_fewer_than_two_instances(mock_httpx_post):
    # Should return immediately without calling httpx
    assert await route_message("hello", []) is None
    assert await route_message("hello", [{"id": 1, "title": "Only One"}]) is None
    mock_httpx_post.assert_not_called()

@pytest.mark.asyncio
async def test_route_message_exact_match(mock_httpx_post, sample_instances):
    # Router returns "2"
    mock_response = MagicMock()
    mock_response.json.return_value = {"response": "2"}
    mock_response.raise_for_status.return_value = None
    mock_httpx_post.return_value = mock_response

    result = await route_message("talk to instance 2", sample_instances)
    assert result == 2
    mock_httpx_post.assert_called_once()

@pytest.mark.asyncio
async def test_route_message_word_match(mock_httpx_post, sample_instances):
    # Router returns a string with a number: "Yes, 3"
    mock_response = MagicMock()
    mock_response.json.return_value = {"response": "Yes, 3"}
    mock_response.raise_for_status.return_value = None
    mock_httpx_post.return_value = mock_response

    result = await route_message("what about 3?", sample_instances)
    assert result == 3

@pytest.mark.asyncio
async def test_route_message_none(mock_httpx_post, sample_instances):
    # Router returns "none"
    mock_response = MagicMock()
    mock_response.json.return_value = {"response": "none"}
    mock_response.raise_for_status.return_value = None
    mock_httpx_post.return_value = mock_response

    result = await route_message("how are you?", sample_instances)
    assert result is None

@pytest.mark.asyncio
async def test_route_message_invalid_id(mock_httpx_post, sample_instances):
    # Router returns an ID that does not exist in instances (e.g. 99)
    mock_response = MagicMock()
    mock_response.json.return_value = {"response": "99"}
    mock_response.raise_for_status.return_value = None
    mock_httpx_post.return_value = mock_response

    result = await route_message("what about 99?", sample_instances)
    assert result is None

@pytest.mark.asyncio
async def test_route_message_timeout(mock_httpx_post, sample_instances):
    # Simulates httpx.TimeoutException
    mock_httpx_post.side_effect = httpx.TimeoutException("Timeout")

    result = await route_message("hello", sample_instances)
    assert result is None

@pytest.mark.asyncio
async def test_route_message_http_error(mock_httpx_post, sample_instances):
    # Simulates an exception like httpx.HTTPStatusError
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError("Error", request=MagicMock(), response=MagicMock())
    mock_httpx_post.return_value = mock_response

    result = await route_message("hello", sample_instances)
    assert result is None
