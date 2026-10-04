from __future__ import annotations

import io
import json

import pytest

from app import slack_api


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def captured(monkeypatch):
    calls: list = []

    def install(result: dict):
        def fake_urlopen(request, timeout):
            calls.append((request, timeout))
            return FakeResponse(json.dumps(result).encode())

        monkeypatch.setattr(slack_api.urllib.request, "urlopen", fake_urlopen)
        return calls

    return install


def test_post_message_sends_thread_reply(captured):
    calls = captured({"ok": True, "ts": "1.2"})

    result = slack_api.post_message("xoxb-token", "C123", "hello", thread_ts="1.0")

    request, timeout = calls[0]
    assert result["ok"] is True
    assert request.full_url == slack_api.POST_MESSAGE_URL
    assert request.get_header("Authorization") == "Bearer xoxb-token"
    assert json.loads(request.data) == {"channel": "C123", "text": "hello", "thread_ts": "1.0"}
    assert timeout == slack_api.REQUEST_TIMEOUT_SECONDS


def test_post_message_without_thread(captured):
    calls = captured({"ok": True})

    slack_api.post_message("xoxb-token", "D123", "hi")

    assert "thread_ts" not in json.loads(calls[0][0].data)


def test_post_message_raises_on_slack_error(captured):
    captured({"ok": False, "error": "channel_not_found"})

    with pytest.raises(slack_api.SlackApiError, match="channel_not_found"):
        slack_api.post_message("xoxb-token", "C404", "hello")
