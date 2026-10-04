"""AWS Lambda entry point.

The same function plays two roles:

1. **Receiver** - invoked synchronously by API Gateway with a Slack Events API
   request. It verifies the Slack signature, answers ``url_verification``
   challenges, and hands real questions off to itself asynchronously so Slack
   gets an HTTP 200 well within its 3-second deadline.
2. **Worker** - invoked asynchronously with a small job payload. It queries the
   Bedrock knowledge base (which can take tens of seconds) and posts the answer
   back to Slack in the originating thread.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from functools import lru_cache
from typing import Any

from app.config import Settings, SlackCredentials, load_slack_credentials
from app.formatting import format_answer
from app.knowledge_base import KnowledgeBaseClient
from app.slack_api import post_message
from app.slack_security import verify_slack_signature

logger = logging.getLogger()
logger.setLevel(logging.INFO)

WORKER_SOURCE = "slack-rag.worker"
MENTION_RE = re.compile(r"<@[A-Z0-9]+>")

EMPTY_QUESTION_REPLY = "Please ask a question after mentioning me, e.g. `@assistant What is RDD?`"
ERROR_REPLY = "Sorry, something went wrong while searching the knowledge base. Please try again."


# --------------------------------------------------------------------------
# Lazily created, cached dependencies (reused across warm invocations).
# Tests replace these functions with fakes.
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


@lru_cache(maxsize=1)
def get_credentials() -> SlackCredentials:
    return load_slack_credentials(get_settings())


@lru_cache(maxsize=1)
def get_knowledge_base() -> KnowledgeBaseClient:
    import boto3

    settings = get_settings()
    return KnowledgeBaseClient(
        boto3.client("bedrock-agent-runtime"),
        settings.knowledge_base_id,
        max_agent_iterations=settings.max_agent_iterations,
        guardrail_id=settings.guardrail_id,
        guardrail_version=settings.guardrail_version,
    )


@lru_cache(maxsize=1)
def get_lambda_client() -> Any:
    import boto3

    return boto3.client("lambda")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if event.get("source") == WORKER_SOURCE:
        return process_question(event["job"])
    return handle_slack_request(event, context)


# --------------------------------------------------------------------------
# Receiver: API Gateway -> Lambda (synchronous, must finish in < 3 s)
# --------------------------------------------------------------------------


def handle_slack_request(event: dict[str, Any], context: Any) -> dict[str, Any]:
    headers = {key.lower(): value for key, value in (event.get("headers") or {}).items()}
    raw_body = _raw_body(event)

    if not verify_slack_signature(
        get_credentials().signing_secret,
        headers.get("x-slack-request-timestamp"),
        headers.get("x-slack-signature"),
        raw_body,
    ):
        logger.warning("Rejected request with an invalid or missing Slack signature")
        return _http_response(401, {"error": "invalid_signature"})

    # Slack retries when it does not get a 200 within 3 s. The original delivery
    # is already being processed, so acknowledge retries without re-processing.
    if "x-slack-retry-num" in headers:
        logger.info(
            "Ignoring Slack retry",
            extra={
                "retry_num": headers["x-slack-retry-num"],
                "retry_reason": headers.get("x-slack-retry-reason"),
            },
        )
        return _http_response(200, {"ok": True})

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        return _http_response(400, {"error": "invalid_json"})

    if body.get("type") == "url_verification":
        return _http_response(200, {"challenge": body.get("challenge")})

    if body.get("type") == "event_callback":
        job = build_job(body)
        if job is not None:
            get_lambda_client().invoke(
                FunctionName=context.invoked_function_arn,
                InvocationType="Event",
                Payload=json.dumps({"source": WORKER_SOURCE, "job": job}).encode("utf-8"),
            )
            logger.info(
                "Queued question",
                extra={"event_id": job["event_id"], "channel": job["channel"]},
            )

    return _http_response(200, {"ok": True})


def build_job(body: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a Slack ``event_callback`` into a worker job, or None to ignore it."""
    slack_event = body.get("event") or {}

    # Ignore our own replies, edits, joins and other message subtypes.
    if slack_event.get("bot_id") or slack_event.get("subtype"):
        return None

    event_type = slack_event.get("type")
    is_direct_message = event_type == "message" and slack_event.get("channel_type") == "im"
    if event_type != "app_mention" and not is_direct_message:
        return None

    # In channels reply in a thread to keep the channel tidy; in DMs reply inline
    # unless the user is already in a thread.
    if is_direct_message:
        thread_ts = slack_event.get("thread_ts")
    else:
        thread_ts = slack_event.get("thread_ts") or slack_event.get("ts")

    return {
        "event_id": body.get("event_id"),
        "channel": slack_event.get("channel"),
        "user": slack_event.get("user"),
        "thread_ts": thread_ts,
        "question": MENTION_RE.sub("", slack_event.get("text", "")).strip(),
    }


# --------------------------------------------------------------------------
# Worker: Lambda -> Bedrock -> Slack (asynchronous)
# --------------------------------------------------------------------------


def process_question(job: dict[str, Any]) -> dict[str, Any]:
    bot_token = get_credentials().bot_token
    channel, thread_ts = job["channel"], job.get("thread_ts")
    log_context = {"event_id": job.get("event_id"), "channel": channel, "user": job.get("user")}

    if not job.get("question"):
        post_message(bot_token, channel, EMPTY_QUESTION_REPLY, thread_ts=thread_ts)
        return {"status": "empty_question"}

    started = time.monotonic()
    try:
        answer = get_knowledge_base().ask(job["question"])
    except Exception:
        logger.exception("Knowledge base query failed", extra=log_context)
        _post_error_reply(bot_token, channel, thread_ts)
        raise  # Surface the failure in the Lambda Errors metric / alarm.

    post_message(bot_token, channel, format_answer(answer), thread_ts=thread_ts)
    logger.info(
        "Answered question",
        extra={
            **log_context,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "source_count": len(answer.sources),
            "answer_chars": len(answer.text),
        },
    )
    return {"status": "answered"}


def _post_error_reply(bot_token: str, channel: str, thread_ts: str | None) -> None:
    try:
        post_message(bot_token, channel, ERROR_REPLY, thread_ts=thread_ts)
    except Exception:
        logger.exception("Failed to post error reply to Slack")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _raw_body(event: dict[str, Any]) -> str:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body).decode("utf-8")
    return body


def _http_response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }
