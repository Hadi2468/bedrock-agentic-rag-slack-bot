# Design: Slack × Bedrock Agentic RAG Assistant

## 1. Context and goals

Teams lose time looking for information spread across PDFs, Word documents, spreadsheets and chat history. The goal is a Slack assistant that answers natural-language questions **only from an approved document set** (35 PDF/Word/Excel files in S3), cites its sources, and runs with minimal operational overhead.

**Goals**

- Grounded answers with citations. When the documents don't cover a question, the assistant says it doesn't know.
- Native Slack experience: `@mention` in channels, direct messages, and threaded replies.
- Serverless and pay-per-use, with no idle infrastructure.
- Secure handling of Slack credentials, and authenticated inbound requests.
- Reproducible deployment (IaC) and automated quality gates (CI).

**Non-goals (for v1)**

- Multi-turn conversational memory (see [Future work](#9-future-work)).
- Per-user document-level access control. All users of the workspace see the same corpus.
- Ingesting Slack messages themselves into the knowledge base.

## 2. Architecture overview

| Component | Responsibility |
|---|---|
| **Slack app** (Events API + Web API) | Delivers `app_mention` and `message.im` events; receives answers via `chat.postMessage`. |
| **API Gateway** (REST, regional) | Public HTTPS endpoint `POST /slack/events`; Lambda proxy integration; stage throttling. |
| **Lambda: receiver path** | Verifies the Slack signature, answers `url_verification`, filters events, enqueues work, returns `200` in < 1 s. |
| **Lambda: worker path** | Calls Bedrock agentic retrieval, formats the answer for Slack, posts it in the thread. |
| **Lambda layer** | boto3/botocore `1.43.108`, which adds the `AgenticRetrieveStream` operation that is missing from the runtime's bundled SDK. |
| **Bedrock managed Knowledge Base** | Parses, chunks, embeds and indexes the S3 documents. At query time it plans, retrieves over several iterations, and generates the answer with a managed foundation model. |
| **S3** | System of record for the source documents (private, Block Public Access on). |
| **Secrets Manager** | Slack bot token and signing secret. |
| **CloudWatch** | Structured JSON logs; alarms on errors and p95 duration. |

Diagrams: see the README ([architecture](README.md#architecture), [request lifecycle](README.md#request-lifecycle)).

## 3. Key design decisions

### D1. Managed Knowledge Base + agentic retrieval instead of a hand-rolled RAG pipeline

*Options considered:* (a) custom pipeline (own chunking, embeddings, vector DB, prompt), (b) classic `RetrieveAndGenerate` (one retrieval pass), (c) **managed KB with `AgenticRetrieveStream`**.

*Decision:* (c). A single retrieval pass struggles with compound questions ("compare X and Y", "which came first…"). Agentic retrieval splits the question into sub-queries, checks whether the retrieved context is enough, iterates (we cap it at 5), and can expand to the full document when a chunk isn't enough. A managed KB also removes the work of running ingestion, an embedding model and a vector store.

*Trade-offs:* less control over chunking and prompt templates, and more latency per question (several model calls). Latency is handled by D3; the iteration cap (`MaxAgentIterations`, 2–5) controls the cost/latency vs. accuracy trade-off.

### D2. Ship a newer boto3 as a Lambda layer

The boto3 bundled with the Lambda Python runtime predates `AgenticRetrieveStream`, so the call fails with `AttributeError`. A layer pins the SDK version (`layer/requirements.txt`) without adding it to the function package, and SAM builds it reproducibly for the right architecture.

### D3. Asynchronous self-invocation to meet Slack's 3-second deadline

Slack expects an HTTP 2xx within **3 seconds** and **retries up to 3 times** otherwise. Agentic retrieval regularly takes longer than that. If the answer were generated inside the HTTP request (as in the first prototype), slow questions would cause Slack retries, and each retry would trigger another Bedrock call and post another answer.

*Decision:* the receiver path does only cheap work (signature check, parsing, filtering). It then invokes the **same function asynchronously** (`InvocationType=Event`) with a small job payload and returns `200` right away. The worker path does the slow work.

*Alternatives:* SQS between receiver and worker (better buffering and DLQ, one more component); Step Functions (overkill for a single step). Self-invocation keeps the deployable unit to one function. Moving to SQS is a contained change if traffic grows.

*Supporting settings:*

- `EventInvokeConfig.MaximumRetryAttempts: 0`: a failed worker must not retry automatically, because that would also post duplicate answers. The user gets an error reply instead and can ask again.
- Requests carrying `X-Slack-Retry-Num` are acknowledged without being processed again. Because the receiver acks quickly, retries should only happen on rare platform hiccups.

### D4. Verify every request with Slack's signing secret

The API Gateway endpoint is public. Every request (including `url_verification`) is checked with Slack's `v0` HMAC-SHA256 scheme over `v0:{timestamp}:{raw_body}`, using `hmac.compare_digest`. Timestamps older than 5 minutes are rejected to prevent replay attacks. The raw body is used exactly as received (base64-decoded if API Gateway encoded it), because re-serialising the JSON would change the bytes and break the signature.

### D5. Credentials in Secrets Manager, loaded once per container

Plaintext tokens in Lambda environment variables show up in the console, in `GetFunctionConfiguration` responses and in screenshots. The function reads one JSON secret at cold start and caches it (`functools.lru_cache`), so warm invocations make no extra calls. Rotating the secret needs no code change; the next cold start picks it up.

### D6. Slack-native presentation

- **Threaded replies** in channels (`thread_ts = event.thread_ts or event.ts`) keep channels tidy. In DMs the bot replies inline unless the user is already in a thread.
- **Markdown → mrkdwn:** LLMs emit GitHub Markdown (`**bold**`, `## headings`, `[text](url)`), which Slack shows literally. A small, tested converter translates the common constructs and leaves code blocks untouched.
- **Citations:** the result event maps answer spans to retrieval results (`citations[].references[].resultIndex`). Only the **cited** results are turned into document names (from the S3 source URI), de-duplicated and capped at 5.
- **Length:** messages are capped below Slack's 40,000-character limit, with an explicit truncation notice.

### D7. Testability through injected / replaceable dependencies

All AWS and Slack clients are created lazily behind small factory functions, and the Bedrock client is passed into `KnowledgeBaseClient`. Tests replace them with in-memory fakes, so the suite runs offline in well under a second with no AWS credentials and no `moto`. Coverage is enforced at ≥ 90% in CI.

## 4. Request lifecycle

1. A user `@mentions` the bot (or DMs it). Slack POSTs a signed `event_callback` to `/slack/events`.
2. **Receiver:** verifies the signature and timestamp. Retries are acknowledged and dropped. Bot messages, edits and other subtypes are ignored.
3. The receiver builds a job `{event_id, channel, user, thread_ts, question}` (mentions removed), invokes itself asynchronously, and returns `200`.
4. **Worker:** calls `AgenticRetrieveStream` with the managed foundation model, `maxAgentIteration`, the KB retriever and an optional guardrail.
5. The worker reads the event stream: `traceEvent` (logged at debug level), `responseEvent` (streamed text), `result` (final answer, results and citations), and any error event, which raises a typed `KnowledgeBaseError`.
6. The answer (final answer if present, otherwise the joined stream, otherwise the "not found" message) is converted to mrkdwn, the cited sources are appended, and the message is posted to the thread.

## 5. Failure modes

| Failure | Behaviour |
|---|---|
| Invalid / missing signature, stale timestamp | `401`, nothing processed, warning logged. |
| Malformed JSON body | `400`. |
| Slack retry delivery | `200`, ignored (original delivery already queued). |
| Bedrock error event (throttling, access denied, KB not found, …) | User gets a friendly error reply in the thread; exception re-raised so the **Errors alarm** fires; no automatic retry. |
| Knowledge base has no relevant content | Model returns a "cannot find" answer, or the fallback text is used. No hallucinated answer. |
| Slack `chat.postMessage` fails | Exception logged and raised (alarm). If posting the error reply also fails, the original exception is preserved. |
| Worker exceeds timeout (120 s) | Lambda timeout → Errors metric/alarm; p95 duration alarm warns before this happens routinely. |

## 6. Security

- **Ingress:** public API, but every request is authenticated with an HMAC signature; stage throttling (20 rps, burst 40) limits abuse and runaway cost.
- **IAM (least privilege):** `secretsmanager:GetSecretValue` on one secret; `bedrock:Retrieve` and `bedrock:GetDocumentContent` on one knowledge base; `bedrock:AgenticRetrieveStream` and `bedrock:InvokeModelWithResponseStream` as documented for agentic retrieval; `lambda:InvokeFunction` on itself only; guardrail actions only when a guardrail is configured.
- **Data at rest:** documents in a private S3 bucket; secrets encrypted by Secrets Manager.
- **Logging hygiene:** logs record event IDs, channel/user IDs, latency, source counts and answer length. Full message bodies and tokens are not logged.
- **Content safety:** optional Bedrock Guardrail (`BLOCK` action) applied during agentic retrieval.

## 7. Observability

- JSON-formatted Lambda logs with structured fields (`event_id`, `latency_ms`, `source_count`, …), queryable with CloudWatch Logs Insights.
- `FunctionErrorsAlarm`: any error in a 5-minute window.
- `FunctionDurationAlarm`: p95 duration > 90 s for 15 minutes (approaching the 120 s timeout).
- Agentic retrieval trace events (planning, retrieval, document expansion) are logged at debug level for troubleshooting retrieval quality.

## 8. Cost and scaling

- **Idle cost is close to zero:** Lambda and API Gateway are pay-per-request; the receiver path runs for milliseconds.
- **Main cost driver:** Bedrock model usage during agentic retrieval. It grows with the number of agent iterations and full-document expansions. `MaxAgentIterations` is the main tuning knob.
- **Scaling:** Lambda scales horizontally per event; API Gateway throttling and Lambda concurrency limits protect Bedrock quotas. For bursty traffic, the next step is SQS between receiver and worker (D3).

## 9. Future work

1. **Conversation memory:** include prior thread messages in `messages`, or use AgentCore Memory session binding, so follow-up questions work.
2. **Exactly-once processing:** DynamoDB conditional write on `event_id` (with TTL) in place of retry-header filtering.
3. **Perceived latency:** post a "Searching…" placeholder, then stream partial text with `chat.update`.
4. **Quality evaluation:** a golden set of question/answer/source triples scored for faithfulness and answer relevance in CI; Bedrock KB evaluation jobs.
5. **Freshness:** S3 event notifications trigger knowledge base ingestion jobs so new documents are searchable within minutes.
6. **Access control:** metadata filters per channel or user group (e.g. HR-only documents).
