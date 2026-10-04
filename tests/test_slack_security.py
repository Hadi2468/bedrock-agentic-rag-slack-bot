from __future__ import annotations

import time

from app.slack_security import verify_slack_signature
from conftest import SIGNING_SECRET, sign

BODY = '{"type":"event_callback"}'


def _verify(headers: dict[str, str], body: str | bytes = BODY, **kwargs) -> bool:
    return verify_slack_signature(
        SIGNING_SECRET,
        headers.get("X-Slack-Request-Timestamp"),
        headers.get("X-Slack-Signature"),
        body,
        **kwargs,
    )


def test_accepts_valid_signature():
    assert _verify(sign(BODY))


def test_accepts_bytes_body():
    assert _verify(sign(BODY), body=BODY.encode())


def test_rejects_tampered_body():
    assert not _verify(sign(BODY), body=BODY + " ")


def test_rejects_wrong_secret():
    assert not _verify(sign(BODY, secret="other-secret"))


def test_rejects_stale_timestamp_to_prevent_replay():
    old = int(time.time()) - 301
    assert not _verify(sign(BODY, timestamp=old))


def test_accepts_timestamp_inside_tolerance():
    headers = sign(BODY, timestamp=1_000_000)
    assert _verify(headers, now=1_000_000 + 299)


def test_rejects_missing_headers():
    assert not _verify({})


def test_rejects_non_numeric_timestamp():
    headers = sign(BODY)
    headers["X-Slack-Request-Timestamp"] = "not-a-number"
    assert not _verify(headers)


def test_rejects_empty_signing_secret():
    headers = sign(BODY)
    assert not verify_slack_signature(
        "", headers["X-Slack-Request-Timestamp"], headers["X-Slack-Signature"], BODY
    )
