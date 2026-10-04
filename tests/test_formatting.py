from __future__ import annotations

from app.formatting import (
    SLACK_MAX_TEXT_LENGTH,
    TRUNCATION_NOTICE,
    format_answer,
    markdown_to_mrkdwn,
)
from app.knowledge_base import Answer


def test_converts_headings_bold_bullets_and_links():
    markdown = (
        "## About the **Course**\n"
        "This is **important** and __also this__.\n"
        "- first item\n"
        "  * nested item\n"
        "See [the docs](https://example.com/docs)."
    )

    assert markdown_to_mrkdwn(markdown) == (
        "*About the Course*\n"
        "This is *important* and *also this*.\n"
        "• first item\n"
        "  • nested item\n"
        "See <https://example.com/docs|the docs>."
    )


def test_leaves_code_blocks_untouched():
    markdown = "```python\n# not a heading\nx = a ** b\n```\n# Heading"

    assert markdown_to_mrkdwn(markdown) == "```python\n# not a heading\nx = a ** b\n```\n*Heading*"


def test_format_answer_appends_sources():
    answer = Answer(text="RDDs are **immutable**.", sources=("spark.pdf", "faq.docx"))

    assert format_answer(answer) == ("RDDs are *immutable*.\n\n*Sources*\n• spark.pdf\n• faq.docx")


def test_format_answer_without_sources():
    assert format_answer(Answer(text="No sources.")) == "No sources."


def test_format_answer_truncates_very_long_answers():
    message = format_answer(Answer(text="x" * (SLACK_MAX_TEXT_LENGTH + 100)))

    assert len(message) == SLACK_MAX_TEXT_LENGTH
    assert message.endswith(TRUNCATION_NOTICE)
