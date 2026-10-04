from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from app import handler
from app.config import SlackCredentials
from app.knowledge_base import Answer
from conftest import SIGNING_SECRET, FakeLambdaClient, sign

BOT_TOKEN = "xoxb-test"


class FakeKnowledgeBase:
    def __init__(self, answer: Answer | None = None, error: Exception | None = None) -> None:
        self.answer = answer or Answer(text="An answer.")
        self.error = error
        self.questions: list[str] = []

    def ask(self, question: str) -> Answer:
        self.questions.append(question)
        if self.error:
            raise self.error
        return self.answer


@pytest.fixture
def deps(monkeypatch):
    """Replace every external dependency of the handler with an in-memory fake."""
    lambda_client = FakeLambdaClient()
    knowledge_base = FakeKnowledgeBase()
    posted: list[dict] = []

    monkeypatch.setattr(
        handler,
        "get_credentials",
        lambda: SlackCredentials(bot_token=BOT_TOKEN, signing_secret=SIGNING_SECRET),
    )
    monkeypatch.setattr(handler, "get_lambda_client", lambda: lambda_client)
    monkeypatch.setattr(handler, "get_knowledge_base", lambda: knowledge_base)

    def fake_post_message(token, channel, text, *, thread_ts=None):
        posted.append({"token": token, "channel": channel, "text": text, "thread_ts": thread_ts})
        return {"ok": True}

    monkeypatch.setattr(handler, "post_message", fake_post_message)

    return SimpleNamespace(
        lambda_client=lambda_client, knowledge_base=knowledge_base, posted=posted
    )


def api_gateway_event(payload: dict, *, headers: dict | None = None, base64_body=False) -> dict:
    body = json.dumps(payload)
    signed = sign(body)
    if headers:
        signed.update(headers)
    if base64_body:
        return {
            "headers": signed,
            "body": base64.b64encode(body.encode()).decode(),
            "isBase64Encoded": True,
        }
    return {"headers": signed, "body": body, "isBase64Encoded": False}


def mention_event(text="<@U0BOT> What is RDD?", **event_overrides) -> dict:
    event = {
        "type": "app_mention",
        "text": text,
        "channel": "C123",
        "user": "U42",
        "ts": "1700000000.000100",
    }
    event.update(event_overrides)
    return {"type": "event_callback", "event_id": "Ev01", "event": event}


# ----------------------------------------------------------------------------
# Receiver
# ----------------------------------------------------------------------------


def test_url_verification_returns_challenge(deps, fake_context):
    event = api_gateway_event({"type": "url_verification", "challenge": "abc123"})

    response = handler.lambda_handler(event, fake_context)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"challenge": "abc123"}


def test_rejects_unsigned_requests(deps, fake_context):
    event = api_gateway_event(mention_event())
    event["headers"]["X-Slack-Signature"] = "v0=forged"

    response = handler.lambda_handler(event, fake_context)

    assert response["statusCode"] == 401
    assert deps.lambda_client.invocations == []


def test_app_mention_is_acknowledged_and_queued_asynchronously(deps, fake_context):
    response = handler.lambda_handler(api_gateway_event(mention_event()), fake_context)

    assert response["statusCode"] == 200
    assert deps.knowledge_base.questions == []  # Not answered inline.
    invocation = deps.lambda_client.invocations[0]
    assert invocation["FunctionName"] == fake_context.invoked_function_arn
    assert invocation["InvocationType"] == "Event"
    payload = json.loads(invocation["Payload"])
    assert payload == {
        "source": handler.WORKER_SOURCE,
        "job": {
            "event_id": "Ev01",
            "channel": "C123",
            "user": "U42",
            "thread_ts": "1700000000.000100",
            "question": "What is RDD?",
        },
    }


def test_base64_encoded_body_is_supported(deps, fake_context):
    event = api_gateway_event(mention_event(), base64_body=True)

    assert handler.lambda_handler(event, fake_context)["statusCode"] == 200
    assert len(deps.lambda_client.invocations) == 1


def test_slack_retries_are_acknowledged_but_not_reprocessed(deps, fake_context):
    event = api_gateway_event(
        mention_event(),
        headers={"X-Slack-Retry-Num": "1", "X-Slack-Retry-Reason": "http_timeout"},
    )

    response = handler.lambda_handler(event, fake_context)

    assert response["statusCode"] == 200
    assert deps.lambda_client.invocations == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"bot_id": "B999"},
        {"subtype": "message_changed"},
        {"type": "reaction_added"},
        {"type": "message", "channel_type": "channel"},
    ],
)
def test_irrelevant_events_are_ignored(deps, fake_context, overrides):
    event = api_gateway_event(mention_event(**overrides))

    assert handler.lambda_handler(event, fake_context)["statusCode"] == 200
    assert deps.lambda_client.invocations == []


def test_invalid_json_returns_400(deps, fake_context):
    body = "not json"
    event = {"headers": sign(body), "body": body}

    assert handler.lambda_handler(event, fake_context)["statusCode"] == 400


# ----------------------------------------------------------------------------
# Job building
# ----------------------------------------------------------------------------


def test_direct_message_replies_inline():
    body = mention_event(text="What is RDD?", type="message", channel_type="im", channel="D1")

    job = handler.build_job(body)

    assert job["question"] == "What is RDD?"
    assert job["thread_ts"] is None


def test_mention_inside_thread_replies_in_same_thread():
    job = handler.build_job(mention_event(thread_ts="1699999999.000001"))

    assert job["thread_ts"] == "1699999999.000001"


def test_all_user_mentions_are_stripped():
    job = handler.build_job(mention_event(text="<@U0BOT>   explain <@U0BOT> caching  "))

    assert job["question"] == "explain  caching"


# ----------------------------------------------------------------------------
# Worker
# ----------------------------------------------------------------------------


def worker_event(question="What is RDD?") -> dict:
    return {
        "source": handler.WORKER_SOURCE,
        "job": {
            "event_id": "Ev01",
            "channel": "C123",
            "user": "U42",
            "thread_ts": "1700000000.000100",
            "question": question,
        },
    }


def test_worker_answers_in_thread(deps, fake_context):
    deps.knowledge_base.answer = Answer(text="**RDD** is a dataset.", sources=("spark.pdf",))

    result = handler.lambda_handler(worker_event(), fake_context)

    assert result == {"status": "answered"}
    assert deps.knowledge_base.questions == ["What is RDD?"]
    assert deps.posted == [
        {
            "token": BOT_TOKEN,
            "channel": "C123",
            "text": "*RDD* is a dataset.\n\n*Sources*\n• spark.pdf",
            "thread_ts": "1700000000.000100",
        }
    ]


def test_worker_prompts_for_a_question_when_empty(deps, fake_context):
    result = handler.lambda_handler(worker_event(question=""), fake_context)

    assert result == {"status": "empty_question"}
    assert deps.knowledge_base.questions == []
    assert deps.posted[0]["text"] == handler.EMPTY_QUESTION_REPLY


def test_worker_reports_failures_to_user_and_reraises(deps, fake_context):
    deps.knowledge_base.error = RuntimeError("bedrock down")

    with pytest.raises(RuntimeError, match="bedrock down"):
        handler.lambda_handler(worker_event(), fake_context)

    assert deps.posted[0]["text"] == handler.ERROR_REPLY


def test_worker_still_raises_original_error_if_error_reply_fails(deps, fake_context, monkeypatch):
    deps.knowledge_base.error = RuntimeError("bedrock down")

    def failing_post(*args, **kwargs):
        raise ConnectionError("slack down")

    monkeypatch.setattr(handler, "post_message", failing_post)

    with pytest.raises(RuntimeError, match="bedrock down"):
        handler.lambda_handler(worker_event(), fake_context)
