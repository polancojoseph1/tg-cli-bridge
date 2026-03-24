import os
import asyncio
from unittest.mock import AsyncMock
from contextlib import contextmanager

# Minimal env so config imports don't crash
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:AAtesttoken")
os.environ.setdefault("ALLOWED_USER_ID", "12345678")
os.environ.setdefault("CLI_RUNNER", "generic")
os.environ.setdefault("CLI_COMMAND", "echo")
os.environ.setdefault("ENV_FILE", "/dev/null")

import pytest
from fastapi.testclient import TestClient

import server

# Disable rate limiting during tests so multiple hits to /query don't trigger 429
server.app.dependency_overrides[server._limiter] = lambda: None
server._limiter.enabled = False

client = TestClient(server.app)

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
