from __future__ import annotations

import pytest

from app.knowledge_base import (
    NO_ANSWER_TEXT,
    KnowledgeBaseClient,
    KnowledgeBaseError,
    extract_sources,
)
from conftest import FakeBedrockAgentRuntime

KB_ID = "KB12345678"


def _result_event(answer: str | None, results=None, citations=None) -> dict:
    generated = {"answer": answer} if answer is not None else {}
    if citations is not None:
        generated["citations"] = citations
    return {"result": {"generatedResponse": generated, "results": results or []}}


def _s3_result(uri: str) -> dict:
    return {
        "content": {"text": "chunk"},
        "metadata": {"x-amz-bedrock-kb-source-uri": uri},
        "sourceRetriever": {"identifier": KB_ID},
    }


def test_build_request_uses_managed_model_and_knowledge_base():
    client = KnowledgeBaseClient(FakeBedrockAgentRuntime([]), KB_ID, max_agent_iterations=3)

    request = client.build_request("What is RDD?")

    assert request["agenticRetrieveConfiguration"] == {
        "foundationModelType": "MANAGED",
        "maxAgentIteration": 3,
    }
    assert request["generateResponse"] is True
    assert request["messages"] == [{"role": "user", "content": {"text": "What is RDD?"}}]
    retriever = request["retrievers"][0]["configuration"]["knowledgeBase"]
    assert retriever == {"knowledgeBaseId": KB_ID}
    assert "policyConfiguration" not in request


def test_build_request_adds_guardrail_when_configured():
    client = KnowledgeBaseClient(
        FakeBedrockAgentRuntime([]), KB_ID, guardrail_id="gr-1", guardrail_version="2"
    )

    request = client.build_request("q")

    assert request["policyConfiguration"] == {
        "bedrockGuardrailConfiguration": {"guardrailId": "gr-1", "guardrailVersion": "2"}
    }


def test_ask_prefers_final_answer_over_streamed_chunks():
    runtime = FakeBedrockAgentRuntime(
        [
            {"traceEvent": {"attributes": {"step": "PLANNING", "status": "InProgress"}}},
            {"responseEvent": {"text": "partial "}},
            {"responseEvent": {"text": "answer"}},
            _result_event("Final answer."),
        ]
    )

    answer = KnowledgeBaseClient(runtime, KB_ID).ask("q")

    assert answer.text == "Final answer."
    assert runtime.requests[0]["messages"][0]["content"]["text"] == "q"


def test_ask_falls_back_to_streamed_text():
    runtime = FakeBedrockAgentRuntime(
        [{"responseEvent": {"text": "Streamed "}}, {"responseEvent": {"text": "only."}}]
    )

    assert KnowledgeBaseClient(runtime, KB_ID).ask("q").text == "Streamed only."


def test_ask_returns_fallback_when_model_returns_nothing():
    runtime = FakeBedrockAgentRuntime([_result_event(None)])

    assert KnowledgeBaseClient(runtime, KB_ID).ask("q").text == NO_ANSWER_TEXT


def test_ask_returns_cited_sources():
    results = [
        _s3_result("s3://docs-bucket/Spark%20Concepts%20and%20Questions.pdf"),
        _s3_result("s3://docs-bucket/hr/Vacation_Policy.docx"),
    ]
    citations = [
        {"startIndex": 0, "endIndex": 5, "references": [{"resultIndex": 1}]},
        {"startIndex": 6, "endIndex": 9, "references": [{"resultIndex": 0}, {"resultIndex": 1}]},
    ]
    runtime = FakeBedrockAgentRuntime([_result_event("Answer", results, citations)])

    answer = KnowledgeBaseClient(runtime, KB_ID).ask("q")

    assert answer.sources == ("Vacation_Policy.docx", "Spark Concepts and Questions.pdf")


@pytest.mark.parametrize(
    "kind",
    ["accessDeniedException", "throttlingException", "resourceNotFoundException"],
)
def test_ask_raises_on_error_events(kind):
    runtime = FakeBedrockAgentRuntime([{kind: {"message": "boom"}}])

    with pytest.raises(KnowledgeBaseError) as excinfo:
        KnowledgeBaseClient(runtime, KB_ID).ask("q")

    assert excinfo.value.kind == kind
    assert "boom" in str(excinfo.value)


def test_extract_sources_ignores_invalid_indices_and_unknown_metadata():
    results = [{"metadata": {"title": "no uri here"}}, _s3_result("https://host/a/b/report.xlsx")]
    citations = [{"references": [{"resultIndex": 0}, {"resultIndex": 7}, {"resultIndex": 1}]}]

    assert extract_sources(results, citations, limit=5) == ("report.xlsx",)


def test_extract_sources_respects_limit():
    results = [_s3_result(f"s3://b/doc{i}.pdf") for i in range(4)]
    citations = [{"references": [{"resultIndex": i} for i in range(4)]}]

    assert extract_sources(results, citations, limit=2) == ("doc0.pdf", "doc1.pdf")
