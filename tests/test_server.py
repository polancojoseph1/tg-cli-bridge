"""Tests for server.py"""
import os
import sys

# Add the project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))

# Minimal env so config imports don't crash
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:AAtesttoken")
os.environ.setdefault("ALLOWED_USER_ID", "12345678")
os.environ.setdefault("CLI_RUNNER", "generic")
os.environ.setdefault("CLI_COMMAND", "echo")
os.environ.setdefault("ENV_FILE", "/dev/null")

import json
import hashlib
import hmac
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient


from server import app

client = TestClient(app)


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
        assert response.json() == {"ok": False, "error": "trigger secret too short — minimum 32 bytes required"}


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
