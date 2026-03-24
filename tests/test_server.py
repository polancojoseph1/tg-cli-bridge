import os
from fastapi.testclient import TestClient

# Minimal env so config imports don't crash
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:AAtesttoken")
os.environ.setdefault("ALLOWED_USER_ID", "12345678")
os.environ.setdefault("CLI_RUNNER", "generic")
os.environ.setdefault("CLI_COMMAND", "echo")

from server import app
import health

client = TestClient(app)

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
