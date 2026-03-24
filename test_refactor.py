import pytest
from runners.base import RunnerBase

class DummyRunner(RunnerBase):
    pass

def test_format_query_result():
    assert DummyRunner.format_query_result(["hello", "world"], b"", b"") == "helloworld"
    assert DummyRunner.format_query_result(["hello", "world"], b"", b"", join_char=" ") == "hello world"
    assert DummyRunner.format_query_result(None, b"hello", b"") == "hello"
    assert DummyRunner.format_query_result(None, b"", b"error") == "[stderr] error"
    assert DummyRunner.format_query_result(None, b"", b"") == "(no response)"

if __name__ == "__main__":
    pytest.main(["-v", "test_refactor.py"])
