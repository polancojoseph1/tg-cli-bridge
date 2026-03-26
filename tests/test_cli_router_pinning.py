import pytest

from runners.base import RunnerBase
import runners.cli_router as cli_router


class DummyRunner(RunnerBase):
    name = "dummy"
    cli_command = ""

    def is_available(self) -> bool:
        return True

    def new_session(self, instance) -> None:
        return None

    async def run(
        self,
        message,
        instance,
        on_progress=None,
        image_path=None,
        memory_context="",
        on_subprocess_started=None,
        chat_id=0,
        user_is_owner=True,
    ) -> str:
        return "ok"

    async def run_query(self, prompt: str, timeout: int = 120) -> str:
        return "ok"


@pytest.fixture
def router(monkeypatch):
    def _fake_create(name: str):
        r = DummyRunner()
        r.name = name
        return r

    monkeypatch.setenv("CLI_ORDER", "antigravity,codex")
    monkeypatch.setattr(cli_router, "_create_runner_by_name", _fake_create)
    return cli_router.CLIRouterRunner()


def test_pin_overrides_last_used(router):
    instance_id = 1
    router._instance_active[instance_id] = "antigravity"
    router.set_preference("codex")
    router._instance_active[instance_id] = "codex"
    order = router._build_try_order(instance_id)
    assert order[0] == "codex"


def test_clear_pin_falls_back_to_preference(router):
    instance_id = 2
    router._instance_active[instance_id] = "antigravity"
    router.set_preference("codex")
    router._instance_active.pop(instance_id, None)
    order = router._build_try_order(instance_id)
    assert order[0] == "codex"
