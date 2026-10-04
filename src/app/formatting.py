"""Convert model output (GitHub-flavoured Markdown) to Slack ``mrkdwn``."""

from __future__ import annotations

import re

from app.knowledge_base import Answer

# chat.postMessage truncates text longer than 40,000 characters.
SLACK_MAX_TEXT_LENGTH = 39_000
TRUNCATION_NOTICE = "\n\n_(answer truncated)_"

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def _bold(match: re.Match[str]) -> str:
    return f"*{match.group(1) or match.group(2)}*"


def markdown_to_mrkdwn(text: str) -> str:
    """Translate the Markdown constructs LLMs commonly emit into Slack mrkdwn.

    Slack renders ``**bold**`` and ``## Heading`` literally, so answers look noisy
    without this step. Fenced code blocks are passed through untouched.
    """
    lines: list[str] = []
    in_code_block = False

    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_code_block = not in_code_block
            lines.append(line)
            continue
        if in_code_block:
            lines.append(line)
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            title = _BOLD_RE.sub(lambda m: m.group(1) or m.group(2), heading.group(1))
            lines.append(f"*{title}*")
            continue

        line = _BULLET_RE.sub(r"\1• ", line)
        line = _BOLD_RE.sub(_bold, line)
        line = _LINK_RE.sub(r"<\2|\1>", line)
        lines.append(line)

    return "\n".join(lines)


def format_answer(answer: Answer) -> str:
    """Render an answer, plus its cited source documents, as a Slack message."""
    message = markdown_to_mrkdwn(answer.text)
    if answer.sources:
        source_lines = "\n".join(f"• {name}" for name in answer.sources)
        message = f"{message}\n\n*Sources*\n{source_lines}"

    if len(message) > SLACK_MAX_TEXT_LENGTH:
        message = message[: SLACK_MAX_TEXT_LENGTH - len(TRUNCATION_NOTICE)] + TRUNCATION_NOTICE
    return message
