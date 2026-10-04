"""Verification of Slack request signatures.

See https://api.slack.com/authentication/verifying-requests-from-slack
"""

from __future__ import annotations

import hashlib
import hmac
import time

# Slack recommends rejecting requests older than five minutes to prevent replay attacks.
DEFAULT_TOLERANCE_SECONDS = 300


def verify_slack_signature(
    signing_secret: str,
    timestamp: str | None,
    signature: str | None,
    body: str | bytes,
    *,
    now: float | None = None,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> bool:
    """Return True if the request was signed by Slack with ``signing_secret``."""
    if not (signing_secret and timestamp and signature):
        return False

    try:
        request_time = int(timestamp)
    except ValueError:
        return False

    current_time = time.time() if now is None else now
    if abs(current_time - request_time) > tolerance_seconds:
        return False

    if isinstance(body, bytes):
        body = body.decode("utf-8")

    base_string = f"v0:{timestamp}:{body}".encode()
    expected = "v0=" + hmac.new(signing_secret.encode(), base_string, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
