"""Local-only API. Presenter routes must never be registered as agent tools."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from functools import lru_cache
from threading import Event, Thread
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import AwareDatetime, BaseModel, Field
from redis import Redis
from redis.exceptions import RedisError

from moniepoint_demo.chat import (
    AnswerGenerator,
    ChatResponse,
    OpenAIAnswerGenerator,
    PolicyChat,
)
from moniepoint_demo.fixtures import CUSTOMER_IDS, demo_customers
from moniepoint_demo.jev import JevJudge, Judge
from moniepoint_demo.memory import IrisMemory, MemorySearch
from moniepoint_demo.models import (
    Approval,
    Contribution,
    Customer,
    NewTransaction,
    PlanChangeRequest,
    ProcessingRecord,
    SavingsPlan,
    SupportSession,
    Transaction,
    TransactionStatus,
    TransactionType,
    World,
)
from moniepoint_demo.policies import PolicyCatalog, PolicyDocument, PolicyStore
from moniepoint_demo.policy_cache import CachePage, Job, PolicyCache, StoreAnswer
from moniepoint_demo.policy_search import (
    Embeddings,
    IndexStatus,
    OpenAIEmbeddings,
    PolicySearch,
    RetrievalRequest,
    SearchRequest,
    SearchResponse,
)
from moniepoint_demo.simulator import (
    DomainError,
    apply_change,
    create_transaction,
    get_plan,
    propose_change,
)
from moniepoint_demo.store import RedisWorldStore
from moniepoint_demo.support_agent import (
    AgentModel,
    CreateSession,
    DecisionRequest,
    MessageRequest,
    OpenAIAgentModel,
    SessionView,
    SupportAgent,
    TurnResponse,
)
from moniepoint_demo.support_service import SupportService
from moniepoint_demo.support_tools import ToolContract, tool_catalog


class PolicyUpdate(BaseModel):
    expected_version: str = Field(min_length=1)
    markdown: str = Field(min_length=1, max_length=100000)


Moment = Annotated[AwareDatetime | None, Query()]


def create_app(
    store: RedisWorldStore | None = None,
    embeddings: Embeddings | None = None,
    judge: Judge | None = None,
    generator: AnswerGenerator | None = None,
    agent_model: AgentModel | None = None,
) -> FastAPI:
    owned = store is None
    database = store or RedisWorldStore(
        Redis.from_url(
            os.environ.get("REDIS_URL", "redis://localhost:6387/0"),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    )

    policies = PolicyStore(database.client, f"{database.key}:policies")

    search = PolicySearch(policies, embeddings or OpenAIEmbeddings())

    cache = PolicyCache(
        search,
        judge or JevJudge(),
        similarity=float(os.environ.get("CACHE_SIMILARITY", "0.75")),
        confidence=float(os.environ.get("JEV_CONFIDENCE", "0.5")),
    )
    chat = PolicyChat(cache, generator or OpenAIAnswerGenerator())
    support_model = agent_model or OpenAIAgentModel()
    memory = (
        IrisMemory.from_environment() if owned and IrisMemory.configured() else None
    )

    @lru_cache(maxsize=3)
    def support_runner(customer_id: str) -> SupportAgent:
        return SupportAgent(
            database.for_customer(customer_id), support_model, chat, memory
        )

    stopping = Event()

    def cache_worker() -> None:
        while not stopping.is_set():
            try:
                cache.process_invalidation()
                cache.process_one()
            except RedisError:
                logging.getLogger(__name__).warning("Cache worker Redis unavailable")
            stopping.wait(0.5)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        for customer_id in CUSTOMER_IDS:
            database.for_customer(customer_id).initialize()
        if not database.client.exists(policies.key):
            policies.publish_bundled()
        cache.initialize()
        worker = Thread(target=cache_worker, daemon=True)
        if owned:
            worker.start()
        try:
            yield
        finally:
            stopping.set()
            if owned:
                worker.join(timeout=60)
        if memory:
            memory.close()
        if owned:
            database.client.close()

    app = FastAPI(
        title="Moniepoint support simulator",
        description=(
            "Fictional SAYT world for a support-agent demonstration. "
            "All amounts are NGN minor units. Times use a simulated UTC clock. "
            "Presenter controls change the world; tools expose evidence. "
            "Local only: no production authentication or money movement."
        ),
        lifespan=lifespan,
    )

    def customer_store(
        customer_id: Annotated[
            str,
            Header(
                alias="X-Demo-Customer-Id",
                description="Presenter-selected demo identity: customer-001 (Ada), "
                "customer-002 (Chidi), customer-003 (Bisi), customer-004 (Emeka), "
                "or customer-005 (Zainab). Not authentication.",
            ),
        ] = "customer-001",
    ) -> RedisWorldStore:
        return database.for_customer(customer_id)

    @app.get("/presenter/customers", tags=["Presenter controls"])
    def customers() -> list[Customer]:
        return demo_customers()

    @app.get("/support/memory/status", tags=["Support agent"])
    def memory_status() -> dict[str, bool]:
        return {"configured": memory is not None}

    @app.get("/support/sessions/{session_id}/memory", tags=["Support agent"])
    def inspect_memory(
        session_id: str,
        selected: Annotated[RedisWorldStore, Depends(customer_store)],
    ) -> MemorySearch:
        session = SupportService(selected).session(session_id)
        if not session.use_memory:
            return MemorySearch()
        if memory is None:
            raise DomainError("Iris not configured", 503)
        return memory.search(
            session.customer_id, "Previous support observations and transactions"
        )

    @app.get("/support/tools", tags=["Presenter controls"])
    def support_tool_contracts() -> list[ToolContract]:
        """Inspect the complete agent tool descriptions and model-visible schemas."""
        return tool_catalog()

    @app.get("/support/sessions", tags=["Support agent"])
    def list_support_sessions(
        selected: Annotated[RedisWorldStore, Depends(customer_store)],
    ) -> list[SupportSession]:
        return sorted(
            selected.read().support_sessions,
            key=lambda session: session.created_at,
            reverse=True,
        )

    @app.post("/support/sessions", tags=["Support agent"], status_code=201)
    def create_support_session(
        request: CreateSession,
        selected: Annotated[RedisWorldStore, Depends(customer_store)],
    ) -> SupportSession:
        if request.use_memory and memory is None:
            raise DomainError("Configure Iris before enabling memory", 503)
        return SupportService(selected).create_session(
            request.use_cache, request.use_memory, request.use_history
        )

    @app.post("/support/sessions/{session_id}/messages", tags=["Support agent"])
    def support_message(
        session_id: str,
        request: MessageRequest,
        selected: Annotated[RedisWorldStore, Depends(customer_store)],
    ) -> TurnResponse:
        """Investigate with tools. Retry the same request_id/message after failure.

        Approval cards are decided through the separate proposal decision endpoint.
        """
        SupportService(selected).session(session_id)
        return support_runner(selected.customer_id).ask(session_id, request)

    @app.post(
        "/support/sessions/{session_id}/proposals/{proposal_id}/decision",
        tags=["Support agent"],
    )
    def support_decision(
        session_id: str,
        proposal_id: str,
        request: DecisionRequest,
        selected: Annotated[RedisWorldStore, Depends(customer_store)],
    ) -> TurnResponse:
        """Record a customer decision, apply if approved, then explain the outcome.

        Application-only event: never an agent tool. Retry the same request ID and
        decision after a failure. The action field reports the service outcome even
        if answer generation fails. Changing proposal settings is not accepted here.
        """
        SupportService(selected).session(session_id)
        return support_runner(selected.customer_id).decide(
            session_id, proposal_id, request
        )

    @app.get("/support/sessions/{session_id}", tags=["Support agent"])
    def inspect_support_session(
        session_id: str,
        selected: Annotated[RedisWorldStore, Depends(customer_store)],
    ) -> SessionView:
        SupportService(selected).session(session_id)
        return support_runner(selected.customer_id).inspect(session_id)

    @app.get("/support/sessions/{session_id}/turns/{turn_id}", tags=["Support agent"])
    def inspect_support_turn(
        session_id: str,
        turn_id: str,
        selected: Annotated[RedisWorldStore, Depends(customer_store)],
    ) -> TurnResponse:
        SupportService(selected).session(session_id)
        return support_runner(selected.customer_id).turn(session_id, turn_id)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code, content={"detail": str(error)}
        )

    @app.exception_handler(RedisError)
    async def database_error(request: Request, error: RedisError) -> JSONResponse:
        return JSONResponse(
            status_code=503, content={"detail": "Demo Redis unavailable"}
        )

    @app.get("/health", tags=["Status"])
    def health() -> dict[str, str]:
        database.client.ping()
        return {"status": "ok"}

    @app.get("/tools/policies", tags=["Product guidance"])
    def list_policies() -> PolicyCatalog:
        """List shared public guidance; no customer-specific account data."""
        return policies.catalog()

    @app.post("/tools/search-policies", tags=["Product guidance"])
    def search_policies(request: RetrievalRequest) -> SearchResponse:
        """Retrieve policy evidence only; chat handles cache checks and generation."""
        return search.search(request)

    @app.post("/chat", tags=["Chat"])
    def ask_policy(request: SearchRequest) -> ChatResponse:
        """Return a policy answer with a cache toggle and complete timing."""
        return chat.ask(request)

    @app.post("/tools/store-answer", tags=["Product guidance"], status_code=202)
    def store_answer(request: StoreAnswer) -> Job:
        """Queue public guidance for worker validation and embedding."""
        return cache.enqueue(request)

    @app.get("/presenter/cache", tags=["Presenter controls"])
    def inspect_cache(
        query: Annotated[str | None, Query(max_length=2000)] = None,
        source_id: str | None = None,
        current: bool | None = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> CachePage:
        """List stored answers; query matches question/answer text case-insensitively.

        Omit current to include both current and stale entries. TTL -1 means no
        expiry. Results cover this embedding namespace and exclude queued jobs.
        """
        return cache.list_entries(query, source_id, current, offset, limit)

    @app.get("/presenter/cache-jobs/{job_id}", tags=["Presenter controls"])
    def cache_job(job_id: str) -> Job:
        return cache.job(job_id)

    @app.post("/presenter/search-policies", tags=["Presenter controls"])
    def inspect_retrieval(request: RetrievalRequest) -> SearchResponse:
        """Inspect retrieval directly, bypassing the semantic cache."""
        return search.search(request)

    @app.put("/presenter/policies/{policy_id}", tags=["Presenter controls"])
    def update_policy(policy_id: str, request: PolicyUpdate) -> IndexStatus:
        """Build search, publish the edit, and queue stale cache cleanup."""
        return search.update_policy(
            policy_id, request.expected_version, request.markdown
        )

    @app.get("/presenter/policy-index", tags=["Presenter controls"])
    def policy_index_status() -> IndexStatus:
        return search.status()

    @app.post("/presenter/policy-index", tags=["Presenter controls"])
    def index_policies() -> IndexStatus:
        """Embed the published corpus and activate its completed search index."""
        return search.rebuild()

    @app.get("/tools/policies/{policy_id}", tags=["Product guidance"])
    def read_policy(policy_id: str) -> PolicyDocument:
        """Read Markdown plus source, scope, retrieval date, and content version."""
        return policies.get(policy_id)

    @app.get(
        "/tools/policies/{policy_id}/markdown",
        tags=["Product guidance"],
        response_class=PlainTextResponse,
    )
    def policy_markdown(policy_id: str) -> PlainTextResponse:
        return PlainTextResponse(
            policies.get(policy_id).markdown, media_type="text/markdown"
        )

    @app.get("/tools/transactions", tags=["Payments"])
    def find_transactions(
        selected: Annotated[RedisWorldStore, Depends(customer_store)],
        since: Moment = None,
        until: Moment = None,
        type: TransactionType | None = None,
        status: TransactionStatus | None = None,
    ) -> list[Transaction]:
        if since and until and since > until:
            raise DomainError("since must not be later than until", 422)
        return [
            tx
            for tx in selected.read().transactions
            if (since is None or tx.occurred_at >= since)
            and (until is None or tx.occurred_at <= until)
            and (type is None or tx.type == type)
            and (status is None or tx.status == status)
        ]

    @app.get("/tools/transactions/{transaction_id}", tags=["Payments"])
    def transaction(
        transaction_id: str,
        selected: Annotated[RedisWorldStore, Depends(customer_store)],
    ) -> Transaction:
        for tx in selected.read().transactions:
            if tx.transaction_id == transaction_id:
                return tx
        raise DomainError("Transaction not found", 404)

    @app.get("/tools/plan", tags=["Savings Plans"])
    def plan(
        selected: Annotated[RedisWorldStore, Depends(customer_store)], at: Moment = None
    ) -> SavingsPlan:
        return get_plan(selected.read(), at)

    @app.get("/tools/processing/{transaction_id}", tags=["SAYT Processing"])
    def processing(
        transaction_id: str,
        selected: Annotated[RedisWorldStore, Depends(customer_store)],
    ) -> ProcessingRecord | None:
        world = selected.read()
        if not any(tx.transaction_id == transaction_id for tx in world.transactions):
            raise DomainError("Transaction not found", 404)
        return next(
            (
                item
                for item in world.processing
                if item.transaction_id == transaction_id
            ),
            None,
        )

    @app.get("/tools/contributions", tags=["Savings Ledger"])
    def contributions(
        selected: Annotated[RedisWorldStore, Depends(customer_store)],
        transaction_id: str | None = None,
        since: Moment = None,
        until: Moment = None,
    ) -> list[Contribution]:
        if since and until and since > until:
            raise DomainError("since must not be later than until", 422)
        return [
            item
            for item in selected.read().contributions
            if (transaction_id is None or item.transaction_id == transaction_id)
            and (since is None or item.posted_at >= since)
            and (until is None or item.posted_at <= until)
        ]

    @app.post("/tools/plan-changes", tags=["Savings Plans"], status_code=201)
    def propose(
        change: PlanChangeRequest,
        selected: Annotated[RedisWorldStore, Depends(customer_store)],
    ) -> Approval:
        return selected.mutate(
            lambda world: propose_change(
                world,
                change.enabled_transaction_types,
                change.conversation_id,
                world.clock + timedelta(seconds=1),
            )
        )

    @app.post("/tools/plan-changes/{approval_id}/apply", tags=["Savings Plans"])
    def apply(
        approval_id: str, selected: Annotated[RedisWorldStore, Depends(customer_store)]
    ) -> SavingsPlan:
        return selected.mutate(
            lambda world: apply_change(
                world,
                approval_id,
                world.clock + timedelta(seconds=1),
            )
        )

    @app.get("/presenter/world", tags=["Presenter controls"])
    def inspect_world(
        selected: Annotated[RedisWorldStore, Depends(customer_store)],
    ) -> World:
        return selected.read()

    @app.post("/presenter/reset", tags=["Presenter controls"])
    def reset(selected: Annotated[RedisWorldStore, Depends(customer_store)]) -> World:
        return selected.reset()

    @app.post("/presenter/transactions", tags=["Presenter controls"], status_code=201)
    def add_transaction(
        request: NewTransaction,
        selected: Annotated[RedisWorldStore, Depends(customer_store)],
    ) -> Transaction:
        return selected.mutate(lambda world: create_transaction(world, request))

    return app


app = create_app()
