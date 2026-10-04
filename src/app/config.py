"""Runtime configuration and secret loading."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Settings:
    """Non-secret configuration, read from Lambda environment variables."""

    knowledge_base_id: str
    max_agent_iterations: int = 5
    guardrail_id: str | None = None
    guardrail_version: str | None = None
    slack_secret_arn: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            knowledge_base_id=os.environ["KNOWLEDGE_BASE_ID"],
            max_agent_iterations=int(os.environ.get("MAX_AGENT_ITERATIONS", "5")),
            guardrail_id=os.environ.get("GUARDRAIL_ID") or None,
            guardrail_version=os.environ.get("GUARDRAIL_VERSION") or None,
            slack_secret_arn=os.environ.get("SLACK_SECRET_ARN") or None,
        )


@dataclass(frozen=True)
class SlackCredentials:
    bot_token: str
    signing_secret: str


def load_slack_credentials(settings: Settings, secrets_client: Any = None) -> SlackCredentials:
    """Load Slack credentials from Secrets Manager.

    The secret must be a JSON object: {"bot_token": "xoxb-...", "signing_secret": "..."}.
    When no secret ARN is configured (local development), fall back to the
    SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET environment variables.
    """
    if settings.slack_secret_arn:
        if secrets_client is None:
            import boto3

            secrets_client = boto3.client("secretsmanager")
        response = secrets_client.get_secret_value(SecretId=settings.slack_secret_arn)
        payload = json.loads(response["SecretString"])
        return SlackCredentials(
            bot_token=payload["bot_token"],
            signing_secret=payload["signing_secret"],
        )

    return SlackCredentials(
        bot_token=os.environ["SLACK_BOT_TOKEN"],
        signing_secret=os.environ["SLACK_SIGNING_SECRET"],
    )
