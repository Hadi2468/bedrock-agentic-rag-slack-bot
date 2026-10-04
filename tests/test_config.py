from __future__ import annotations

import json

import pytest

from app.config import Settings, SlackCredentials, load_slack_credentials


class FakeSecretsManager:
    def __init__(self, secret: dict) -> None:
        self.secret = secret
        self.requested: list[str] = []

    def get_secret_value(self, SecretId: str) -> dict:  # noqa: N803 - boto3 casing
        self.requested.append(SecretId)
        return {"SecretString": json.dumps(self.secret)}


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_BASE_ID", "KB1")
    monkeypatch.setenv("MAX_AGENT_ITERATIONS", "3")
    monkeypatch.setenv("GUARDRAIL_ID", "")
    monkeypatch.setenv("SLACK_SECRET_ARN", "arn:secret")

    settings = Settings.from_env()

    assert settings == Settings(
        knowledge_base_id="KB1",
        max_agent_iterations=3,
        guardrail_id=None,
        guardrail_version=None,
        slack_secret_arn="arn:secret",
    )


def test_settings_requires_knowledge_base_id(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_BASE_ID", raising=False)

    with pytest.raises(KeyError):
        Settings.from_env()


def test_credentials_loaded_from_secrets_manager():
    secrets = FakeSecretsManager({"bot_token": "xoxb-test", "signing_secret": "shh"})
    settings = Settings(knowledge_base_id="KB1", slack_secret_arn="arn:secret")

    credentials = load_slack_credentials(settings, secrets_client=secrets)

    assert credentials == SlackCredentials(bot_token="xoxb-test", signing_secret="shh")
    assert secrets.requested == ["arn:secret"]


def test_credentials_fall_back_to_env_for_local_development(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-local")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "local-secret")

    credentials = load_slack_credentials(Settings(knowledge_base_id="KB1"))

    assert credentials == SlackCredentials(bot_token="xoxb-local", signing_secret="local-secret")
