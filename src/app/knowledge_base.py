"""Question answering over a Bedrock managed knowledge base using agentic retrieval."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

logger = logging.getLogger(__name__)

NO_ANSWER_TEXT = "I couldn't find enough information in the knowledge base to answer that question."

RETRIEVER_DESCRIPTION = (
    "Internal company documents (PDF, Word and Excel). "
    "Use this knowledge base to answer the user's question using retrieved documents."
)

# Error members of the AgenticRetrieveStream event stream.
STREAM_ERROR_EVENTS = (
    "accessDeniedException",
    "badGatewayException",
    "conflictException",
    "dependencyFailedException",
    "internalServerException",
    "resourceNotFoundException",
    "serviceQuotaExceededException",
    "throttlingException",
    "validationException",
)


class KnowledgeBaseError(RuntimeError):
    """An error event was received on the Bedrock response stream."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind


@dataclass(frozen=True)
class Answer:
    text: str
    sources: tuple[str, ...] = ()


class KnowledgeBaseClient:
    """Thin wrapper around ``bedrock-agent-runtime.agentic_retrieve_stream``."""

    def __init__(
        self,
        bedrock_agent_runtime: Any,
        knowledge_base_id: str,
        *,
        max_agent_iterations: int = 5,
        guardrail_id: str | None = None,
        guardrail_version: str | None = None,
        max_sources: int = 5,
    ) -> None:
        self._client = bedrock_agent_runtime
        self._knowledge_base_id = knowledge_base_id
        self._max_agent_iterations = max_agent_iterations
        self._guardrail_id = guardrail_id
        self._guardrail_version = guardrail_version
        self._max_sources = max_sources

    def build_request(self, question: str) -> dict[str, Any]:
        request: dict[str, Any] = {
            "agenticRetrieveConfiguration": {
                "foundationModelType": "MANAGED",
                "maxAgentIteration": self._max_agent_iterations,
            },
            "generateResponse": True,
            "messages": [{"role": "user", "content": {"text": question}}],
            "retrievers": [
                {
                    "configuration": {
                        "knowledgeBase": {"knowledgeBaseId": self._knowledge_base_id},
                    },
                    "description": RETRIEVER_DESCRIPTION,
                }
            ],
        }
        if self._guardrail_id and self._guardrail_version:
            request["policyConfiguration"] = {
                "bedrockGuardrailConfiguration": {
                    "guardrailId": self._guardrail_id,
                    "guardrailVersion": self._guardrail_version,
                }
            }
        return request

    def ask(self, question: str) -> Answer:
        response = self._client.agentic_retrieve_stream(**self.build_request(question))

        streamed_chunks: list[str] = []
        final_answer: str | None = None
        results: list[Mapping[str, Any]] = []
        citations: list[Mapping[str, Any]] = []

        for event in response["stream"]:
            if "responseEvent" in event:
                streamed_chunks.append(event["responseEvent"].get("text", ""))
            elif "result" in event:
                result = event["result"]
                generated = result.get("generatedResponse") or {}
                if generated.get("answer"):
                    final_answer = generated["answer"]
                citations = generated.get("citations") or []
                results = result.get("results") or []
            elif "traceEvent" in event:
                attributes = event["traceEvent"].get("attributes", {})
                logger.debug(
                    "Agentic retrieval trace",
                    extra={"step": attributes.get("step"), "status": attributes.get("status")},
                )
            else:
                _raise_for_error_event(event)

        text = (final_answer or "".join(streamed_chunks)).strip() or NO_ANSWER_TEXT
        return Answer(text=text, sources=extract_sources(results, citations, self._max_sources))


def _raise_for_error_event(event: Mapping[str, Any]) -> None:
    for kind in STREAM_ERROR_EVENTS:
        if kind in event:
            raise KnowledgeBaseError(kind, event[kind].get("message", "no message"))


def extract_sources(
    results: Sequence[Mapping[str, Any]],
    citations: Iterable[Mapping[str, Any]],
    limit: int,
) -> tuple[str, ...]:
    """Return de-duplicated document names for the results the answer actually cites."""
    names: list[str] = []
    for citation in citations:
        for reference in citation.get("references", []):
            index = reference.get("resultIndex")
            if not isinstance(index, int) or not 0 <= index < len(results):
                continue
            name = _document_name(results[index].get("metadata") or {})
            if name and name not in names:
                names.append(name)
    return tuple(names[:limit])


def _document_name(metadata: Mapping[str, Any]) -> str | None:
    """Best-effort extraction of a file name from knowledge base result metadata."""
    candidates = sorted(metadata.items(), key=lambda item: "source-uri" not in item[0].lower())
    for _, value in candidates:
        if isinstance(value, str) and value.startswith(("s3://", "https://", "http://")):
            return unquote(value.rstrip("/").rsplit("/", 1)[-1]) or None
    return None
