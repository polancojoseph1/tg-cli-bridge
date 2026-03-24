"""Tests for server.py"""
import os
import sys
import asyncio
import json
import hashlib
import hmac
from unittest.mock import patch, AsyncMock

# Add the project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

# Minimal env so config imports don't crash
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:AAtesttoken")
os.environ.setdefault("ALLOWED_USER_ID", "12345678")
os.environ.setdefault("CLI_RUNNER", "generic")
os.environ.setdefault("CLI_COMMAND", "echo")
os.environ.setdefault("ENV_FILE", "/dev/null")

from fastapi.testclient import TestClient

import server
import health
from server import app

# Disable rate limiting during tests so multiple hits to /query don't trigger 429
server.app.dependency_overrides[server._limiter] = lambda: None
server._limiter.enabled = False

client = TestClient(app)


# --- health endpoint tests ---

def test_health_endpoint():
    """Test that the /health endpoint returns the expected structure and status code."""
    # Reset health stats for deterministic testing
    health._start_time = 0.0
    health._message_count = 0
    health._last_message_time = None
    health.init()

    response = client.get("/health")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert "uptime_seconds" in data
    assert data["message_count"] == 0
    assert data["last_message_time"] is None

def test_health_endpoint_after_message():
    """Test the /health endpoint after a message has been processed."""
    health.record_message()

    response = client.get("/health")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert "uptime_seconds" in data
    assert data["message_count"] == 1
    assert data["last_message_time"] is not None


# --- status endpoint test ---

def test_status_endpoint():
    # Ensure health is initialized properly so _start_time is set
    health.init()

    response = client.get("/status")

    # Assert successful response
    assert response.status_code == 200

    data = response.json()

    # Assert all keys expected from health.get_status() are present
    assert "status" in data
    assert "uptime_seconds" in data
    assert "message_count" in data
    assert "last_message_time" in data
    assert "cli_runner" in data
    assert "bot_name" in data
    assert "cli_available" in data

    # Assert some specific values where possible
    assert data["status"] == "ok"
    assert data["message_count"] >= 0


# --- direct_query tests ---

def test_direct_query_no_auth():
    server.INTERNAL_API_KEY = "test-key"
    response = client.post("/query", json={"prompt": "hello"})
    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "Unauthorized"}

def test_direct_query_invalid_auth():
    server.INTERNAL_API_KEY = "test-key"
    response = client.post(
        "/query",
        json={"prompt": "hello"},
        headers={"X-API-Key": "wrong-key"}
    )
    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "Unauthorized"}

def test_direct_query_success():
    server.INTERNAL_API_KEY = "test-key"
    server.runner.run_query = AsyncMock(return_value="mocked response")
    response = client.post(
        "/query",
        json={"prompt": "hello"},
        headers={"X-API-Key": "test-key"}
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "response": "mocked response"}

def test_direct_query_timeout():
    server.INTERNAL_API_KEY = "test-key"
    server.runner.run_query = AsyncMock(side_effect=asyncio.TimeoutError())
    response = client.post(
        "/query",
        json={"prompt": "hello", "timeout_secs": 120},
        headers={"X-API-Key": "test-key"}
    )
    assert response.status_code == 504
    assert response.json() == {
        "ok": False,
        "error": "AI response timed out after 120s",
        "response": ""
    }

def test_direct_query_general_error():
    server.INTERNAL_API_KEY = "test-key"
    server.runner.run_query = AsyncMock(side_effect=Exception("boom"))
    response = client.post(
        "/query",
        json={"prompt": "hello"},
        headers={"X-API-Key": "test-key"}
    )
    assert response.status_code == 500
    assert response.json() == {
        "ok": False,
        "error": "boom",
        "response": ""
    }


# --- trigger_webhook tests ---

# A dummy TriggerDefinition-like object to mock trigger lookups
class MockTrigger:
    def __init__(self, id="test_trigger", enabled=True, config=None):
        self.id = id
        self.enabled = enabled
        self.config = config or {"secret": "super_secret_key_that_is_32_bytes_long"}


def test_trigger_webhook_not_available():
    with patch("server._triggers_available", False):
        response = client.post("/triggers/webhook/test_trigger")
        assert response.status_code == 503
        assert response.json() == {"ok": False, "error": "triggers not available"}

def test_trigger_webhook_not_found():
    with patch("server._triggers_available", True), \
         patch("server.trigger_registry.get_trigger", return_value=None):
        response = client.post("/triggers/webhook/nonexistent")
        assert response.status_code == 404
        assert response.json() == {"ok": False, "error": "trigger not found"}

def test_trigger_webhook_disabled():
    mock_trigger = MockTrigger(enabled=False)
    with patch("server._triggers_available", True), \
         patch("server.trigger_registry.get_trigger", return_value=mock_trigger):
        response = client.post("/triggers/webhook/disabled_trigger")
        assert response.status_code == 200
        assert response.json() == {"ok": False, "error": "trigger disabled"}

def test_trigger_webhook_missing_secret():
    mock_trigger = MockTrigger(config={"secret": ""})
    with patch("server._triggers_available", True), \
         patch("server.trigger_registry.get_trigger", return_value=mock_trigger):
        response = client.post("/triggers/webhook/test_trigger")
        assert response.status_code == 401
        assert response.json() == {"ok": False, "error": "trigger has no secret configured"}

def test_trigger_webhook_short_secret():
    mock_trigger = MockTrigger(config={"secret": "short"})
    with patch("server._triggers_available", True), \
         patch("server.trigger_registry.get_trigger", return_value=mock_trigger):
        response = client.post("/triggers/webhook/test_trigger")
        assert response.status_code == 401
        assert response.json() == {"ok": False, "error": "trigger secret too short \u2014 minimum 32 bytes required"}


def test_trigger_webhook_invalid_signature():
    secret = "a" * 32
    mock_trigger = MockTrigger(config={"secret": secret})
    with patch("server._triggers_available", True), \
         patch("server.trigger_registry.get_trigger", return_value=mock_trigger):
        response = client.post(
            "/triggers/webhook/test_trigger",
            headers={"X-Hub-Signature-256": "sha256=invalid_signature"},
            json={"data": "test"}
        )
        assert response.status_code == 401
        assert response.json() == {"ok": False, "error": "invalid signature"}

def test_trigger_webhook_event_filter_mismatch():
    secret = "a" * 32
    mock_trigger = MockTrigger(config={"secret": secret, "event": "push"})
    payload = b'{"data": "test"}'
    signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    with patch("server._triggers_available", True), \
         patch("server.trigger_registry.get_trigger", return_value=mock_trigger):
        response = client.post(
            "/triggers/webhook/test_trigger",
            headers={
                "X-Hub-Signature-256": signature,
                "X-GitHub-Event": "pull_request"
            },
            content=payload
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True, "skipped": "event 'pull_request' != 'push'"}

def test_trigger_webhook_branch_filter_mismatch():
    secret = "a" * 32
    mock_trigger = MockTrigger(config={"secret": secret, "branch": "main"})
    payload = json.dumps({"ref": "refs/heads/feature"}).encode()
    signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    with patch("server._triggers_available", True), \
         patch("server.trigger_registry.get_trigger", return_value=mock_trigger):
        response = client.post(
            "/triggers/webhook/test_trigger",
            headers={"X-Hub-Signature-256": signature},
            content=payload
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True, "skipped": "branch 'feature' != 'main'"}



def test_trigger_webhook_success_no_filters():
    secret = "a" * 32
    mock_trigger = MockTrigger(config={"secret": secret})
    payload = b'{"data": "test"}'
    signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    with patch("server._triggers_available", True), \
         patch("server.trigger_registry.get_trigger", return_value=mock_trigger), \
         patch("server.trigger_worker.fire", new_callable=AsyncMock) as mock_fire:
        mock_fire.return_value = True

        response = client.post(
            "/triggers/webhook/test_trigger",
            headers={"X-Hub-Signature-256": signature},
            content=payload
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        mock_fire.assert_awaited_once_with("test_trigger")

def test_trigger_webhook_success_with_filters():
    secret = "a" * 32
    mock_trigger = MockTrigger(config={"secret": secret, "event": "push", "branch": "main"})
    payload = json.dumps({"ref": "refs/heads/main"}).encode()
    signature = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    with patch("server._triggers_available", True), \
         patch("server.trigger_registry.get_trigger", return_value=mock_trigger), \
         patch("server.trigger_worker.fire", new_callable=AsyncMock) as mock_fire:
        mock_fire.return_value = True

        response = client.post(
            "/triggers/webhook/test_trigger",
            headers={
                "X-Hub-Signature-256": signature,
                "X-GitHub-Event": "push"
            },
            content=payload
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        mock_fire.assert_awaited_once_with("test_trigger")
