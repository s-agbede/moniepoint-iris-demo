# Technical guide

An inspectable SAYT simulator with Payments, Savings Plans, Processing, and
Ledger tools, a LangGraph support agent, a React chat UI, and optional Iris memory.
The simulator contains facts and rules, not scripted customer conversations or
predetermined agent responses.

## Run

```sh
docker compose up -d --wait
uv sync
uv run --env-file .env uvicorn moniepoint_demo.api:app --host 127.0.0.1 --port 8017
```

Open [interactive API documentation](http://127.0.0.1:8017/docs).
[Inspect all simulator state](http://127.0.0.1:8017/presenter/world).

Redis is available only on loopback port 6387. Its data lives in `.local/redis/`
(ignored by Git), so restarting the app or container preserves the world.
Override `REDIS_URL` if needed. The API is a local presenter application without
production authentication; don't bind it to a public interface.

## Inspect the first increment

Use **Try it out** in the API documentation. Select a fictional customer using the `X-Demo-Customer-Id` header shown on
**Try it out**. Omit it to use Ada (`customer-001`). The application will supply
this selection to agent tools; the agent will not choose a customer.

| Customer | ID | Initial SAYT plan | Transactions |
|---|---|---|---|
| Ada | `customer-001` | Save NGN 500 per card payment | 6 |
| Chidi | `customer-002` | Save NGN 250 per transfer or card payment | 6 |
| Bisi | `customer-003` | Save NGN 100 per airtime or bill payment | 6 |
| Emeka | `customer-004` | Save NGN 500 per card payment | 3 |
| Zainab | `customer-005` | Save NGN 250 per transfer or card payment | 3 |

A **savings plan** is the rule defining which transaction types trigger savings
and the fixed amount saved on each eligible transaction. It is separate from the
ledger, which records actual savings contributions. All five customers are
fictional and use fixed amounts only in this demo.

Use `GET /presenter/customers` to list them. Across the fixtures there are successful contributions, excluded transaction types,
stale events, and failed/pending payments. Each
customer has independent state in Redis. Reset affects only the selected customer.
The header is a local-demo identity selector, not an authentication mechanism.

The walkthrough below uses Ada.

1. Read `/tools/transactions/txn-001`: the transfer completed.
2. Read `/tools/plan`: version 1 permits card payments, not transfers.
3. Read `/tools/processing/txn-001`: the transaction type was excluded.
4. Read `/tools/contributions?transaction_id=txn-001`: there is no contribution.
5. Create a support session using `POST /support/sessions`.
6. Ask the agent to propose adding transfers while preserving card savings and
   show the approval card through `/support/sessions/{session_id}/messages`.
7. Submit `{"decision":"approve","request_id":"approve-1"}` to
   `/support/sessions/{session_id}/proposals/{proposal_id}/decision` using the
   card's ID. The backend applies the exact proposal and returns the new plan.
8. Create a new transaction through `/presenter/transactions`:

```json
{
  "transaction_id": "txn-002",
  "type": "transfer",
  "amount_minor": 1500000,
  "currency": "NGN",
  "status": "completed",
  "occurred_at": "2026-10-21T10:10:00Z",
  "delivery_delay_seconds": 5
}
```

9. Inspect its processing record and ledger contribution. The contribution is
   50000 kobo (NGN 500), while the earlier transaction remains unchanged.
10. Create another transaction with ID `txn-003`, time `2026-10-21T11:00:00Z`,
    and delay `180`. It produces a stale-event record and no contribution,
    although transfers remain enabled.
11. Query `/tools/plan?at=2026-10-21T10:00:00Z` to see the historical plan.
12. Use `POST /presenter/reset` to restore the initial world before another run.

This is a manual inspection guide, not executable dialogue or an agent plan.
You can choose other transaction types, dates, amounts, and delays.

## Simulator boundaries

- The two-minute freshness window is an explicit demo assumption, not a verified
  Moniepoint threshold.
- Plan eligibility uses transaction occurrence time in this simulation. Version
  history is retained; changing a plan never retroactively saves from old events.
- For completed transactions, `occurred_at` is completion/event-production time.
  Pending and failed transactions do not emit completion events.
- Delay is simulated immediately by advancing the world clock; no background
  worker sleeps for three minutes. Real Iris extraction will use real wall time.
- A ledger entry represents a simulated contribution, not a complete accounting
  system: balance checks, debit accounts, fees, and interest are outside this slice.
- Unknown transactions return 404; an existing transaction with no processing
  record returns null. An empty ledger result is evidence, not a diagnosis.
- Repeating an identical transaction is idempotent. Reusing its ID with different
  facts is rejected. Applied approvals are idempotent; stale approvals are refused.
- Presenter routes (reset, transaction creation, full world inspection)
  must never be registered as agent tools.
- Five customer worlds are separately scoped. Tests check that one customer cannot
  read another customer's transactions or apply their approvals. The local identity
  selector is not a production access-control system.

## Validate

With the dedicated Redis container running:

```sh
uv run pytest -q
uv run ruff check .
uv run mypy src tests
```

API tests exercise real Redis using unique per-customer test keys, then delete only those keys.
They cover the full change/verification/return-visit flow and app-restart persistence.
Some API tests accept `TEST_REDIS_URL`; the full suite expects local Redis on port 6387.

## Code map

- `models.py`: typed records and request validation.
- `simulator.py`: generic state transitions and minimal domain-test fixture.
- `fixtures.py`: five fictional customers and 24 sample transactions.
- `store.py`: Redis persistence with optimistic concurrency.
- `api.py`: service tools and separate presenter controls.

## Iris memory

Configure `AGENT_MEMORY_URL`, `AGENT_MEMORY_STORE_ID`, and
`AGENT_MEMORY_API_KEY` in the ignored local `.env`. Start the API with
`uv run --env-file .env` so it loads these values. The integration uses the
`redis-agent-memory` Python SDK and a store with the `support_observation`
custom memory type enabled (customer reports, verified findings, and action outcomes).

Enable **Long-term memory** before creating a **New session**. The setting belongs to
that session; existing sessions remain unchanged. LangGraph checkpoints preserve
conversation state. Iris searches historical observations across sessions for the
same backend-bound customer, filtering by owner and memory type. Historical
observations guide investigation; live services remain authoritative and memories
cannot grant approval.

**Behind the answer** shows retrieved memories, redacted events confirmed written
to Iris, and lookup/recording timings. **Check extracted observations** performs a
fresh top-five search for that customer; it is not a complete inventory. Event
writes are included in response time; managed long-term extraction happens later
according to the store's cadence. Recorded events do not mean extraction is done.
Start another session after extraction to demonstrate recall with a new checkpoint.

The local redactor covers email, common credential labels, and phone/account-like
number patterns before events leave for Iris. This is a demonstration guard, not
complete PII detection; names, addresses, and arbitrary sensitive prose are not
fully covered. LangGraph checkpoints retain the original conversation. Use only
fictional data in this demo. Returned memory text and attributes are also redacted.

Memory failures are visible and do not block support. Partially confirmed event
writes are not automatically replayed by the application. Completed request
replays do not record again, but this is not an exactly-once delivery guarantee
across crashes. `GET /support/memory/status` reports configuration availability;
`GET /support/sessions/{session_id}/memory` exposes the customer-scoped search.

## Sources

- [SAYT architecture](https://engineering.moniepoint.com/save-as-you-transact-how-we-built-a-savings-product-that-reacts-to-millions-of-events)
- [Managing a SAYT plan](https://support.moniepoint.com/topics/savings-198/managing-your-sayt-plan-362)

The public guidance confirms configurable transaction types. The agent-assisted
approval API is our simulation, not an asserted Moniepoint API.

## Product-guidance documents

Twelve concise Markdown summaries of official Moniepoint help articles are
stored under `src/moniepoint_demo/knowledge/`. The four SAYT documents are in
`sayt/`:

- `overview.md`: what SAYT does and the documented version differences.
- `create-plan.md`: choosing contributions and triggering transaction types.
- `manage-plan.md`: updating settings, pausing, and viewing activity.
- `withdrawals.md`: the published withdrawal procedure.

Eight additional documents provide distinct, sometimes overlapping support topics:

- `transfers/not-received.md`: successful transfer not received by the beneficiary.
- `transfers/wrong-account.md`: mistaken recipient and conditional recall.
- `transfers/beneficiaries.md`: adding and removing saved recipients.
- `cards/lost-stolen.md`: protecting and replacing a missing card.
- `cards/limits.md`: changing daily, weekly and monthly card limits.
- `cards/disputes.md`: reporting disputes and the documented filing window.
- `pos/failed-debit.md`: failed payment with customer debited, from the operator's perspective.
- `pos/partial-transfer.md`: successful withdrawal leg but failed onward transfer.

POS operator guidance is explicitly scoped separately from cardholder guidance.
Temporary outage notices are not treated as ongoing explanations. Published
conditions on recalls and reversal timelines are preserved.

These are source-backed guidance summaries, not new bank policies or simulator
rules. They intentionally omit unsettled rate/fee claims and contain no expected
scenario diagnoses. `manifest.json` records source URLs, scope, and retrieval dates.

On first initialization the application publishes this corpus atomically to Redis under
`moniepoint-demo:world:policies`. The API reads the stored Markdown from Redis:

- `GET /tools/policies`: catalog, document versions, and corpus version.
- `GET /tools/policies/{policy_id}`: full document and metadata.
- `GET /tools/policies/{policy_id}/markdown`: raw Markdown.

For example: `/tools/policies/sayt-manage-plan`.

Use `PUT /presenter/policies/{policy_id}` to edit a published document. Restarting
preserves published edits. Bundled Markdown seeds a new database only. Document
and corpus versions are SHA-256 fingerprints, not Moniepoint release identifiers.
Cache hits check source versions before reuse. See the publication workflow below.

Guidance is shared across customers and is unaffected by customer resets. It
contains no account data, customer memories, or authority to execute actions.


## Policy retrieval and chat

Redis 8.4 supplies exact FLAT vector search and BM25 keyword search. Policies use
OpenAI `text-embedding-3-small` with 1536 dimensions. Python combines rankings
using reciprocal rank fusion (constant 60), returning up to three documents.
Source footers are excluded from embedding text and preserved in returned evidence.
The retrieval similarity floor defaults to 0.25; it is not calibrated confidence.

Configure `.env` from `.env.example`, then run:

```sh
uv run --env-file .env uvicorn moniepoint_demo.api:app --host 127.0.0.1 --port 8017
```

In Swagger at `/docs`:

- `POST /presenter/policy-index`: embed published documents and build an index.
- `GET /presenter/policy-index`: inspect index readiness and corpus/model match.
- `POST /tools/search-policies`: retrieve evidence only, without caching or generation.
- `POST /chat`: receive a complete policy answer with caching enabled or disabled.

### Compare complete answers with and without caching

```json
{"query": "How do I change my weekly card spending limit?", "use_cache": false}
```

Send this to `/chat`, then change `use_cache` to true. The default is true.

- Off: retrieve policies and call the answering LLM every time; do not automatically
  nominate answers for storage. Existing cache entries are preserved.
- On, accepted hit: verify source versions and ask Jev whether the answer fully
  applies; return the answer without calling the generation model.
- On, miss/rejection: retrieve policies, generate an answer, and optionally queue
  supported general guidance for cache admission. A nomination is not an approved write.

Responses contain `answer`, `answer_source` (generated/cache), source links and
versions, decision `trace`, `generation_model` (null on hits), and `cache_job_id`
when nominated. Generation is a separate application component using configurable
`ANSWER_MODEL` (default `gpt-4.1-mini`). The model selects supporting source IDs and
whether to nominate; the app validates references against retrieved documents.

This is single-turn policy Q&A, not the account-investigation agent. It has no
customer-service tools, chat history, approvals or Iris memory connected yet.
Account-specific requests must acknowledge that live checks are needed. No answer
text or demo conversation is hardcoded into the application.

### Timings

`timings.total_ms` surrounds the entire chat call, including lookup attempts,
retrieval, generation, citation checks and queue submission. `stages_ms` exposes
`cache_embedding`, `cache_lookup`, `source_validation`, `jev`, `retrieval`, `receipt`,
`generation`, `cache_enqueue`, and `citations`. Skipped stages show zero.
`retrieval_timings` breaks down embedding, vector/keyword search, index validation
and ranking; these are already included in the outer retrieval time.

The timer uses a monotonic clock. Total includes ordinary orchestration overhead;
provider timings include their network calls. HTTP serialization and browser network
time are excluded. Background admission/embedding/storage runs separately and is
not included in response latency. A cache-off comparison now includes generation.

### Cache storage and verification

`GET /presenter/cache` lists stored questions, answers, source versions, freshness
(`current`) and remaining `ttl_seconds` (-1 means no expiry). Optional filters:
`query` matches question/answer text case-insensitively, `source_id` matches a cited
document, and `current=true/false` selects current/stale entries. Use `offset` and
`limit` (default 20, maximum 100) to paginate; `total` counts filtered entries.
For example: `/presenter/cache?query=card&source_id=cards-limits&current=true`.
This scans the small demo's active embedding namespace without invoking models;
queued nominations are separate. Pagination is not a frozen snapshot.

The cache uses local Redis Search, not a separate LangCache Cloud deployment.
Jev uses OpenRouter's Decisions API and `OPENROUTER_API_KEY`. See `.env.example`
for `JEV_MODEL`, `JEV_CONFIDENCE` (0.5) and `CACHE_SIMILARITY` (0.75). These are
initial settings, not accuracy guarantees. Jev sees the new and original questions
and the cached answer; rejection, uncertainty, low confidence or errors fall back
to retrieval and generation.

`GET /presenter/cache-jobs/{job_id}` exposes queued, processing, stored, duplicate, rejected
or failed status, including the admission decision. A background worker screens
nominated answers for public, self-contained, source-supported guidance using
Jev, then embeds and stores them. Redis Streams persist nominations before the
request returns. Pending jobs can be reclaimed after 60 seconds, with at most
three attempts; stable entry IDs make replays idempotent.

Before storing an admitted answer, the worker retrieves up to three similar cache
entries and asks Jev whether a current existing answer fully answers the new
question. A confident acceptance keeps the existing entry and marks the job
`duplicate`, with `duplicate_of` and `dedup_verdict` for inspection. Rejection,
uncertainty, low confidence or verifier unavailability allow a separate entry.
An exact question with the same source snapshot uses a stable key, replacing the
previous approved answer instead of accumulating entries. Job IDs remain unique.
Semantic deduplication assumes this demo's single worker; concurrent workers can
still store equivalent paraphrases.

Source versions are checked before and after reuse verification and before storage.
Changed cited documents block reuse; unrelated edits do not. Added/deleted document
IDs conservatively invalidate older answers. A background worker consumes publication events from Redis
Streams to remove stale entries. Entries expire after 24 hours, retrieval receipts after
15 minutes and job records after seven days. The small demo's stream is untrimmed.

The low-level `/tools/store-answer` endpoint remains for nominations tied to a
server-issued retrieval receipt. The chat application supplies these automatically;
the server derives original question and source versions, rejecting invented references.
An email/long-number/identifier screen plus Jev admission are demo safeguards, not
complete PII detection or proof of correctness. Do not submit real secrets. The support integration uses session-bound tools and an additional regex redactor;
neither replaces production authorization or comprehensive PII controls.

### Updating and evaluating retrieval

Publish policy changes through the update endpoint, which rebuilds before activation.
Retrieval refuses an outdated index. A new index activates only after all records are ready;
failed builds preserve the previous one. Old successful index generations remain
for this small demo.

```sh
uv run --env-file .env python -m moniepoint_demo.evaluate_search
```

The twelve cases in `evals/policy-retrieval.json` check expected policies in the top
three. They are smoke evaluations, not a general quality guarantee, and never drive
the application. Automated tests use controlled providers and real Redis to verify
cache, generation and failure boundaries without requiring external credentials.

### Support agent with Redis checkpoints

`GET /support/tools` exposes all eight reviewed descriptions and argument schemas.
Customer/session identity and cache settings are injected by the application and
are absent from model-visible arguments. This endpoint is inspection-only, under Presenter controls in Swagger. The agent
receives the same definitions directly; it does not call this HTTP endpoint.

The session service and typed tool adapters now enforce managed-proposal ownership,
replacement, presentation before consent, decline, plan-version checks, and
idempotent application. The legacy presenter approval endpoint has been removed;
legacy direct apply calls cannot operate on managed proposals. Resetting a customer world removes its support sessions.

See `docs/superpowers/specs/2026-10-21-support-orchestration-design.md` for the
reviewed graph and tool catalogue. Optional Iris integration is described above.


The support investigation loop now runs through LangGraph with RedisSaver, using
`AGENT_MODEL` (default `gpt-6.1-sol`) with low reasoning through the OpenAI Responses API. No LangSmith
service or Iris memory is required for this baseline.

1. `POST /support/sessions` with `{"use_cache": false}`. Set the existing
   `X-Demo-Customer-Id` header to choose the fictional customer; default is Ada.
2. Copy the returned `id` into `POST /support/sessions/{session_id}/messages`:

   ```json
   {
     "message": "My transfer txn-001 completed but I cannot see savings from it. Can you investigate?",
     "request_id": "my-first-question"
   }
   ```

3. Use a new request ID for each new message. Retry the exact same ID and message
   to resume a failed request or retrieve its completed result without rerunning it.
4. Inspect `GET /support/sessions/{session_id}` for saved history and the current
   pending card, and `GET /support/sessions/{session_id}/turns/{turn_id}` for traces.

A turn returns the answer, status, observed tool calls/results, optional pending
proposal, and timings. Policy citations and cache traces are preserved inside the
`ask_policy_question` tool result. Times are milliseconds: `total_ms` accumulates
server processing over request attempts (excluding time between retries), while
model/tool stages report checkpointed successful node work. Failed calls or replayed
work can therefore add to total without appearing in the stage sums. HTTP transport
and response serialization are excluded. No private model reasoning is returned.

Sessions retain messages across process restarts. Redis checkpoints preserve graph
progress, while session records bind identity and business records govern proposals.
Customer reset removes those session records, making old checkpoints inaccessible;
physical checkpoint cleanup and retention policies are not part of this demo yet.
There is no context summarization or truncation in this increment.

Execution is bounded to 8 model rounds, 20 tool executions, and a 180-second budget
checked between calls (an in-flight call may finish later). One turn per session is
allowed at a time. A Redis lock rejects concurrent requests; after a process crashes
mid-turn its lock expires within 10 minutes. Retry the unfinished request before
starting another. No automatic recovery worker is included.

### Approve or decline a proposal

After the agent returns an `approval` card, submit to
`POST /support/sessions/{session_id}/proposals/{proposal_id}/decision`:

```json
{"decision": "approve", "request_id": "decision-1"}
```

Use `decline` instead to reject the proposal. This endpoint accepts no settings;
it validates the stored proposal's customer, session, status, and plan version.
Approval goes directly to the protected graph node before the model explains the
outcome. The model has no approve/apply tool. A chat "yes" is never consent.
The old `/presenter/approvals/{id}/confirm` endpoint is removed.

The response's `action.status` is `applied`, `declined`, or `rejected`, with the
applied plan where relevant. Read this independently of conversational `status`:
an action can succeed even if its explanation fails. Retry the identical decision
and request ID to recover; successful application is idempotent. A mismatched
request payload, wrong session/customer, stale or superseded proposal is rejected.
A service conflict occurring after initial validation is reported as a rejected
action and explained by the agent. Decision events appear in session inspection
under `application_events`, separately from customer messages, with request/turn
and proposal references. `timings.stages_ms.action` measures service decision work.

## Support studio UI

Run the backend on port 8017 as above, then:

```sh
cd frontend
npm install
npm run dev
```

Open http://127.0.0.1:5173. Vite proxies `/api` to the local FastAPI service;
API keys stay on the backend. `npm run build` type-checks and builds the UI.

Choose a customer and create a session. The semantic-cache setting applies to
new sessions; the active session's setting is displayed above the chat. Session
history is customer-scoped. The sidebar shows actual tool arguments/results and
backend stage timings; hiding it does not hide response timing. Approval cards
submit separate application decisions. Business action outcomes remain visible
if generating their explanation fails. Retry preserves the exact request ID and
payload, including across a browser reload on the same origin.

Browser-observed answer duration is measured through response rendering and saved
locally by turn ID. Another browser displays explicitly labelled server processing
time instead. Retrying a stored request does not replace the original duration
with cache/retrieval latency. Sessions created before the ordered UI history index
show their original conversation without invented timing or traces. The UI uses
GET `/support/sessions` and the `turns` collection on session inspection; both are
bound to the selected customer. Only one client should operate a session at a time.

This is a local demo, not authentication: `X-Demo-Customer-Id` selects a fictional
customer. New session does not reset customer data or clear the policy cache.

Browser verification includes a real proposal/decline conversation plus a
fixture-only approval failure/retry check in `frontend/tests/approval-smoke.js`.
With Playwright CLI open on the UI, run that file's function with `run-code`.
The fixture test intercepts only browser requests and leaves real plans unchanged.

## Updating policies without a stale-answer window

Use `PUT /presenter/policies/{policy_id}` with `expected_version` from
`GET /tools/policies/{policy_id}` and the replacement `markdown`. This presenter
endpoint is not an agent tool. It currently edits existing documents only.

The service embeds and builds a replacement index while the existing policy stays
active, then atomically publishes the policy snapshot, ready index pointer, and a
Redis Streams change event. Concurrent edits return 409; failed indexing leaves
the existing snapshot active. Each accepted edit currently reindexes the small
12-document corpus. Requests already in flight can fail their version check and
need retrying, rather than return stale guidance.

The background cache worker consumes changes using a persistent cursor, checks
entry source versions, and deletes stale entries. Cleanup is idempotent and can
resume after restart. Unrelated entries remain valid; delayed events do not delete
answers referencing the current document versions. Read-time version validation
continues protecting requests before cleanup finishes. The stream and old search
indexes are retained for this demo; retention/compaction is not implemented.

Redis is authoritative after initialization: restarting no longer overwrites
published edits with bundled files. Edit via the endpoint to coordinate publication
and search. Direct changes to Redis or Markdown files bypass this workflow.

### Demonstrating statelessness

For a new support session, turn **Short-term memory**, **Long-term memory**, and
**Semantic cache** off, then click **New session**. Send a distinctive reference
word, then ask what word you supplied. The prior messages stay visible in the UI
but are not supplied to the model on the next turn. Turn short-term memory on
and start another session to compare.

The API setting is `use_history: false` on `POST /support/sessions` (default true,
including existing sessions). Iris recall remains independently controlled by
`use_memory`. Current-turn tool messages remain available, and checkpoints still
support retrying an interrupted turn. Backend approval state and service checks
remain authoritative; this mode does not disable authorization or erase records.
