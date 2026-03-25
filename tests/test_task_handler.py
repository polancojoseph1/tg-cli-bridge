import os

# Setup dummy environment to avoid config import errors
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy_token")
os.environ.setdefault("ALLOWED_USER_ID", "123456")
os.environ.setdefault("CLI_RUNNER", "dummy")
os.environ.setdefault("CLI_COMMAND", "dummy")


import task_handler

def test_ensure_file_creates_file(tmp_path, monkeypatch):
    """Test that _ensure_file creates the task file and its parent directories if they don't exist."""
    # Use tmp_path to mock the task file location
    dummy_task_file = tmp_path / "Goals" / "TASKS.md"
    monkeypatch.setattr(task_handler, "TASK_FILE", dummy_task_file)

    assert not dummy_task_file.exists()

    task_handler._ensure_file()

    assert dummy_task_file.exists()
    content = dummy_task_file.read_text(encoding="utf-8")
    assert content == task_handler._HEADER

def test_ensure_file_does_not_overwrite(tmp_path, monkeypatch):
    """Test that _ensure_file does not overwrite an existing file."""
    dummy_task_file = tmp_path / "Goals" / "TASKS.md"
    dummy_task_file.parent.mkdir(parents=True, exist_ok=True)
    dummy_task_file.write_text("Existing content", encoding="utf-8")

    monkeypatch.setattr(task_handler, "TASK_FILE", dummy_task_file)

    task_handler._ensure_file()

    content = dummy_task_file.read_text(encoding="utf-8")
    assert content == "Existing content"
    assert content != task_handler._HEADER

def test_add_task(tmp_path, monkeypatch):
    """Test that add_task functions correctly and ensures the file is created."""
    dummy_task_file = tmp_path / "Goals" / "TASKS.md"
    monkeypatch.setattr(task_handler, "TASK_FILE", dummy_task_file)

    result = task_handler.add_task("Test task")
    assert "Task #1 added: Test task" in result

    content = dummy_task_file.read_text(encoding="utf-8")
    assert "Test task" in content

def test_list_tasks(tmp_path, monkeypatch):
    """Test listing tasks."""
    dummy_task_file = tmp_path / "Goals" / "TASKS.md"
    monkeypatch.setattr(task_handler, "TASK_FILE", dummy_task_file)

    task_handler.add_task("Test task 1")
    task_handler.add_task("Test task 2")

    output = task_handler.list_tasks()
    assert "Test task 1" in output
    assert "Test task 2" in output

def test_done_task(tmp_path, monkeypatch):
    """Test completing a task."""
    dummy_task_file = tmp_path / "Goals" / "TASKS.md"
    monkeypatch.setattr(task_handler, "TASK_FILE", dummy_task_file)

    task_handler.add_task("Test task 1")
    task_handler.add_task("Test task 2")

    result = task_handler.done_task(1)
    assert "Completed and removed task #1: Test task 1" in result

    output = task_handler.list_tasks()
    assert "Test task 1" not in output
    assert "Test task 2" in output
