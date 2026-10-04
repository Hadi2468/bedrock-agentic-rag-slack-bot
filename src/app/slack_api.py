"""Minimal Slack Web API client (stdlib only, no extra dependencies)."""

from __future__ import annotations

import json
import urllib.request
from typing import Any

POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
REQUEST_TIMEOUT_SECONDS = 10


class SlackApiError(RuntimeError):
    """Raised when the Slack Web API returns ``ok: false``."""


def post_message(
    bot_token: str,
    channel: str,
    text: str,
    *,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    """Post ``text`` to ``channel``, optionally as a reply in ``thread_ts``."""
    payload: dict[str, Any] = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts

    request = urllib.request.Request(  # noqa: S310 - fixed https URL
        POST_MESSAGE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise SlackApiError(f"Slack API error: {result.get('error', 'unknown_error')}")

    return result
