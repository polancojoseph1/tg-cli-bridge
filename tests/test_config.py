"""Tests for config.py — runs without any real credentials."""
import os
import pytest


def _set_minimal_env(**overrides):
    """Set minimal valid environment variables for config tests."""
    base = {
        "TELEGRAM_BOT_TOKEN": "1234567890:AAtesttoken",
        "ALLOWED_USER_ID": "12345678",
        "ALLOWED_USER_IDS": "",        # always reset to avoid cross-test leakage
        "CLI_RUNNER": "generic",
        "CLI_COMMAND": "echo",
        "ENV_FILE": "/dev/null",        # prevent loading a real .env file
    }
    base.update(overrides)
    for k, v in base.items():
        os.environ[k] = v


class TestValidateConfig:
    def test_valid_config_returns_no_errors(self, monkeypatch):
        _set_minimal_env()
        # Re-import to pick up env changes
        import importlib
        import config
        importlib.reload(config)
        errors = config.validate_config()
        # generic runner skips binary check
        assert errors == []

    def test_missing_token_returns_error(self, monkeypatch):
        _set_minimal_env(TELEGRAM_BOT_TOKEN="")
        import importlib
        import config
        importlib.reload(config)
        errors = config.validate_config()
        assert any("TELEGRAM_BOT_TOKEN" in e for e in errors)

    def test_missing_user_id_returns_error(self, monkeypatch):
        _set_minimal_env(ALLOWED_USER_ID="0")
        import importlib
        import config
        importlib.reload(config)
        errors = config.validate_config()
        assert any("ALLOWED_USER_ID" in e for e in errors)

    def test_invalid_runner_returns_error(self, monkeypatch):
        _set_minimal_env(CLI_RUNNER="invalid_runner")
        import importlib
        import config
        importlib.reload(config)
        errors = config.validate_config()
        assert any("CLI_RUNNER" in e for e in errors)


class TestAllowedUserIds:
    def test_single_user_id(self, monkeypatch):
        _set_minimal_env(ALLOWED_USER_ID="99999", ALLOWED_USER_IDS="")
        import importlib
        import config
        importlib.reload(config)
        assert 99999 in config.ALLOWED_USER_IDS

    def test_multiple_user_ids(self, monkeypatch):
        _set_minimal_env(ALLOWED_USER_IDS="111,222,333")
        import importlib
        import config
        importlib.reload(config)
        assert {111, 222, 333}.issubset(config.ALLOWED_USER_IDS)


class TestDataDir:
    def test_default_data_dir(self, monkeypatch):
        if "TG_BRIDGE_DATA_DIR" in os.environ:
            del os.environ["TG_BRIDGE_DATA_DIR"]
        import importlib
        import config
        importlib.reload(config)
        assert config.DATA_DIR.endswith(".bridgebot")

    def test_custom_data_dir(self, monkeypatch):
        os.environ["TG_BRIDGE_DATA_DIR"] = "/tmp/my-bridge-data"
        import importlib
        import config
        importlib.reload(config)
        assert config.DATA_DIR == "/tmp/my-bridge-data"
