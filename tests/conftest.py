from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

SIGNING_SECRET = "test-signing-secret"


def sign(body: str, timestamp: int | None = None, secret: str = SIGNING_SECRET) -> dict[str, str]:
    """Build the headers Slack would send for ``body``."""
    ts = str(timestamp if timestamp is not None else int(time.time()))
    digest = hmac.new(secret.encode(), f"v0:{ts}:{body}".encode(), hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": f"v0={digest}"}


class FakeBedrockAgentRuntime:
    """Stands in for boto3's bedrock-agent-runtime client."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.requests: list[dict[str, Any]] = []

    def agentic_retrieve_stream(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return {"stream": iter(self.events)}


@dataclass
class FakeLambdaClient:
    invocations: list[dict[str, Any]] = field(default_factory=list)

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.invocations.append(kwargs)
        return {"StatusCode": 202}


@dataclass
class FakeContext:
    invoked_function_arn: str = "arn:aws:lambda:us-east-1:123456789012:function:slack-rag-handler"


@pytest.fixture
def fake_context() -> FakeContext:
    return FakeContext()
