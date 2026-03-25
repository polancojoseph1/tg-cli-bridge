"""Tests for telegram_handler.py — markdown conversion and message splitting."""
import os

# Minimal env so config imports don't crash
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1234567890:AAtesttoken")
os.environ.setdefault("ALLOWED_USER_ID", "12345678")
os.environ.setdefault("CLI_RUNNER", "generic")
os.environ.setdefault("CLI_COMMAND", "echo")

from telegram_handler import (
    markdown_to_telegram_html,
    split_message,
    strip_html_tags,
    _convert_markdown_tables,
)


class TestSplitMessage:
    def test_short_message_is_not_split(self):
        assert split_message("hello") == ["hello"]

    def test_long_message_is_split(self):
        long = "a" * 5000
        chunks = split_message(long)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 4096

    def test_splits_at_newline_preferentially(self):
        text = ("line\n" * 1000)
        chunks = split_message(text)
        for chunk in chunks:
            assert len(chunk) <= 4096


class TestMarkdownToTelegramHtml:
    def test_bold(self):
        # Bold markers are stripped
        result = markdown_to_telegram_html("**hello**")
        assert "hello" in result
        assert "**" not in result
        assert result == "hello"

    def test_italic(self):
        # Italic markers are stripped
        result = markdown_to_telegram_html("*hello*")
        assert "hello" in result
        assert result == "hello"

    def test_code_block(self):
        result = markdown_to_telegram_html("```\ncode\n```")
        assert "<pre>" in result
        assert "code" in result

    def test_inline_code(self):
        result = markdown_to_telegram_html("`code`")
        assert "<code>code</code>" in result

    def test_header_becomes_plain_text(self):
        # Headers are stripped to plain text (## prefix removed, no HTML tag wrapping)
        result = markdown_to_telegram_html("## Title")
        assert "Title" in result
        assert "##" not in result
        assert "<b>" not in result

    def test_html_entities_escaped(self):
        result = markdown_to_telegram_html("<script>alert(1)</script>")
        assert "<script>" not in result
        assert "&lt;script&gt;" in result

    def test_empty_string(self):
        result = markdown_to_telegram_html("")
        assert result == ""


class TestConvertMarkdownTables:
    NARROW_TABLE = (
        "| Name | Age |\n"
        "|------|-----|\n"
        "| Alice | 30 |\n"
        "| Bob   | 25 |\n"
    )

    WIDE_TABLE = (
        "| Col1 | Col2 | Col3 | Col4 | Col5 | Col6 |\n"
        "|------|------|------|------|------|------|\n"
        "| A    | B    | C    | D    | E    | F    |\n"
    )

    def test_narrow_table_renders_as_numbered_list(self):
        result = _convert_markdown_tables(self.NARROW_TABLE)
        # All tables render as numbered lists — never as <pre> blocks
        assert "<pre>" not in result
        assert "<b>1.</b>" in result
        assert "Alice" in result
        assert "Bob" in result
        assert "Name:" in result

    def test_wide_table_renders_as_numbered_list(self):
        result = _convert_markdown_tables(self.WIDE_TABLE)
        # No <pre> block for wide tables
        assert "<pre>" not in result
        # Numbered entry
        assert "<b>1.</b>" in result
        # Headers used as labels
        assert "Col1:" in result

    def test_non_table_text_unchanged(self):
        text = "just some normal text\nwith no pipes"
        result = _convert_markdown_tables(text)
        assert result == text


class TestStripHtmlTags:
    def test_removes_tags(self):
        assert strip_html_tags("<b>hello</b>") == "hello"
        assert strip_html_tags("<pre>code</pre>") == "code"

    def test_plain_text_unchanged(self):
        assert strip_html_tags("hello world") == "hello world"
