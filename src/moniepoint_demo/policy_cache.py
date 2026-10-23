"""Policy-only semantic cache, retrieval receipts and durable background nominations."""

import json
import logging
import re
from hashlib import sha256
from time import perf_counter
from typing import Literal, cast
from uuid import uuid4

from pydantic import Field, ValidationError
from redis.exceptions import RedisError, ResponseError

from moniepoint_demo.jev import Judge, Verdict
from moniepoint_demo.models import Record
from moniepoint_demo.policy_search import PolicySearch, SearchRequest, SearchResponse
from moniepoint_demo.simulator import DomainError
from moniepoint_demo.timings import Timings

logger = logging.getLogger(__name__)


class Source(Record):
    id: str
    version: str


class Receipt(Record):
    query: str
    sources: list[Source]
    catalog: str


class StoreAnswer(Record):
    receipt_id: str = Field(min_length=1, max_length=64)
    answer: str = Field(min_length=1, max_length=8000)
    source_ids: list[str] = Field(min_length=1, max_length=3)


class Entry(Receipt):
    id: str
    answer: str


class InspectedEntry(Entry):
    current: bool
    ttl_seconds: int


class CachePage(Record):
    entries: list[InspectedEntry]
    total: int
    offset: int
    limit: int


def entry_identity(entry: Receipt) -> str:
    """Stable within the embedding namespace; never merge distinct source snapshots."""
    payload = {
        "query": entry.query.strip(),
        "catalog": entry.catalog,
        "sources": sorted((s.id, s.version) for s in entry.sources),
    }
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class Job(Record):
    id: str
    status: Literal["queued", "processing", "stored", "duplicate", "rejected", "failed"]
    entry: Entry
    duplicate_of: str | None = None
    dedup_verdict: Verdict | None = None
    attempts: int = 0
    verdict: Verdict | None = None
    reason: str | None = None


class Trace(Record):
    reason: str
    entry_id: str | None = None
    similarity: float | None = None
    verdict: Verdict | None = None


class CachedSearch(Record):
    timings: Timings = Field(default_factory=Timings)
    answer: str | None = None
    sources: list[Source] = Field(default_factory=list)
    retrieval: SearchResponse | None = None
    receipt_id: str | None = None
    trace: list[Trace] = Field(default_factory=list)


def sensitive(text: str) -> bool:
    # Conservative demo screen, not a complete PII detector. Jev also checks admission.
    return bool(
        re.search(
            r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|\d[\d ()+-]{8,}\d|"
            r"\b(?:customer|txn|account)-\w+",
            text,
        )
    )


class PolicyCache:
    def __init__(
        self,
        searcher: PolicySearch,
        judge: Judge,
        similarity: float = 0.75,
        confidence: float = 0.5,
    ) -> None:
        if not 0 <= similarity <= 1 or not 0 <= confidence <= 1:
            raise ValueError("Cache thresholds must be between zero and one")
        self.searcher = searcher
        self.judge = judge
        self.client = searcher.policies.client
        fingerprint = sha256(
            f"{searcher.embeddings.model}:{searcher.embeddings.dimensions}".encode()
        ).hexdigest()[:16]
        self.root = f"{searcher.policies.key}:cache:{fingerprint}"
        self.index = f"{self.root}:index"
        self.prefix = f"{self.root}:entry:"
        self.queue = f"{self.root}:writes"
        self.group = "writers"
        self.consumer = uuid4().hex
        self.similarity = similarity
        self.confidence = confidence

    def initialize(self) -> None:
        try:
            self.client.execute_command(  # type: ignore[no-untyped-call]
                "FT.CREATE",
                self.index,
                "ON",
                "HASH",
                "PREFIX",
                1,
                self.prefix,
                "SCHEMA",
                "embedding",
                "VECTOR",
                "FLAT",
                6,
                "TYPE",
                "FLOAT32",
                "DIM",
                self.searcher.embeddings.dimensions,
                "DISTANCE_METRIC",
                "COSINE",
            )
        except ResponseError as error:
            if "Index already exists" not in str(error):
                raise
        try:
            self.client.xgroup_create(self.queue, self.group, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    def process_invalidation(self) -> int:
        """Replay failed cleanup using a durable single-worker cursor."""
        cursor_key = f"{self.root}:invalidation-cursor"
        cursor = cast(str | None, self.client.get(cursor_key)) or "0-0"
        batches = cast(
            list[tuple[str, list[tuple[str, dict[str, str]]]]],
            self.client.xread(
                {f"{self.searcher.policies.key}:changes": cursor}, count=1
            ),
        )
        removed = 0
        for _, events in batches:
            for event_id, _ in events:
                # Check current provenance so delayed events cannot delete new answers.
                for key in self.client.scan_iter(match=f"{self.prefix}*", count=100):
                    payload = cast(str | None, self.client.hget(key, "payload"))
                    if payload and not self.current(Entry.model_validate_json(payload)):
                        removed += cast(int, self.client.delete(key))
                self.client.set(cursor_key, event_id)
        return removed

    def catalog(self) -> str:
        ids = sorted(doc.id for doc in self.searcher.policies.read().documents)
        return sha256("\n".join(ids).encode()).hexdigest()

    def list_entries(
        self,
        query: str | None = None,
        source_id: str | None = None,
        current: bool | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> CachePage:
        """Inspect the small demo cache; pagination is not a frozen snapshot."""
        entries: list[InspectedEntry] = []
        corpus = self.searcher.policies.read()
        versions = {doc.id: doc.version for doc in corpus.documents}
        catalog = sha256("\n".join(sorted(versions)).encode()).hexdigest()
        for key in set(self.client.scan_iter(match=f"{self.prefix}*", count=100)):
            payload = cast(str | None, self.client.hget(key, "payload"))
            ttl = cast(int, self.client.ttl(key))
            if payload is None or ttl == -2:
                continue
            entry = Entry.model_validate_json(payload)
            if (
                query
                and query.casefold() not in f"{entry.query}\n{entry.answer}".casefold()
            ):
                continue
            if source_id and not any(s.id == source_id for s in entry.sources):
                continue
            fresh = entry.catalog == catalog and all(
                versions.get(s.id) == s.version for s in entry.sources
            )
            if current is not None and fresh != current:
                continue
            entries.append(
                InspectedEntry(**entry.model_dump(), current=fresh, ttl_seconds=ttl)
            )
        entries.sort(key=lambda entry: entry.id)
        return CachePage(
            entries=entries[offset : offset + limit],
            total=len(entries),
            offset=offset,
            limit=limit,
        )

    def current(self, entry: Receipt) -> bool:
        corpus = self.searcher.policies.read()
        versions = {doc.id: doc.version for doc in corpus.documents}
        catalog = sha256("\n".join(sorted(versions)).encode()).hexdigest()
        return entry.catalog == catalog and all(
            versions.get(source.id) == source.version for source in entry.sources
        )

    def search(self, request: SearchRequest) -> CachedSearch:
        started = perf_counter()
        timings = Timings(
            stages_ms=dict.fromkeys(
                [
                    "cache_embedding",
                    "cache_lookup",
                    "source_validation",
                    "jev",
                    "retrieval",
                    "receipt",
                ],
                0.0,
            )
        )
        result = self._search(request, timings)
        timings.total_ms = (perf_counter() - started) * 1000
        result.timings = timings
        return result

    def _search(self, request: SearchRequest, timings: Timings) -> CachedSearch:
        trace: list[Trace] = []
        if not request.use_cache:
            trace.append(Trace(reason="cache_bypassed"))
        elif sensitive(request.query):
            trace.append(Trace(reason="sensitive_query_cache_bypassed"))
        else:
            try:
                with timings.measure("cache_embedding"):
                    vectors = self.searcher.embeddings.embed([request.query])
                if len(vectors) != 1:
                    raise DomainError("Invalid embedding response", 502)
                with timings.measure("cache_lookup"):
                    rows = self.client.execute_command(  # type: ignore[no-untyped-call]
                        "FT.SEARCH",
                        self.index,
                        "*=>[KNN 3 @embedding $v AS distance]",
                        "PARAMS",
                        2,
                        "v",
                        self.searcher.vector_bytes(vectors[0]),
                        "SORTBY",
                        "distance",
                        "RETURN",
                        2,
                        "payload",
                        "distance",
                        "LIMIT",
                        0,
                        3,
                        "DIALECT",
                        2,
                    )
                for offset in range(1, len(rows), 2):
                    row = rows[offset + 1]
                    fields = dict(zip(row[::2], row[1::2], strict=True))
                    similarity = 1 - float(fields["distance"])
                    if similarity < self.similarity:
                        continue
                    entry = Entry.model_validate_json(fields["payload"])
                    with timings.measure("source_validation"):
                        sources_current = self.current(entry)
                    if not sources_current:
                        trace.append(
                            Trace(
                                reason="stale_sources",
                                entry_id=entry.id,
                                similarity=similarity,
                            )
                        )
                        continue
                    with timings.measure("jev"):
                        verdict = self.judge.decide(
                            {
                                "new_question": request.query,
                                "cached_question": entry.query,
                                "cached_answer": entry.answer,
                            },
                            "reuse",
                        )
                    accepted = (
                        verdict.choice == "accept"
                        and verdict.confidence >= self.confidence
                    )
                    if accepted:
                        with timings.measure("source_validation"):
                            sources_current = self.current(entry)
                    if accepted and sources_current:
                        trace.append(
                            Trace(
                                reason="cache_hit",
                                entry_id=entry.id,
                                similarity=similarity,
                                verdict=verdict,
                            )
                        )
                        return CachedSearch(
                            answer=entry.answer, sources=entry.sources, trace=trace
                        )
                    reason = (
                        "jev_rejected"
                        if verdict.choice != "accept"
                        else "low_confidence"
                        if not accepted
                        else "stale_sources"
                    )
                    trace.append(
                        Trace(
                            reason=reason,
                            entry_id=entry.id,
                            similarity=similarity,
                            verdict=verdict,
                        )
                    )
            except (DomainError, RedisError, ValidationError) as error:
                trace.append(Trace(reason="cache_or_verifier_unavailable"))
                logger.warning("Policy cache fallback: %s", type(error).__name__)
        if not trace:
            trace.append(Trace(reason="cache_miss"))
        with timings.measure("retrieval"):
            retrieval = self.searcher.search(request)
        with timings.measure("receipt"):
            receipt_id = uuid4().hex
            receipt = Receipt(
                query=request.query,
                catalog=self.catalog(),
                sources=[
                    Source(id=hit.document.id, version=hit.document.version)
                    for hit in retrieval.results
                ],
            )
            self.client.set(
                f"{self.root}:receipt:{receipt_id}", receipt.model_dump_json(), ex=900
            )
        return CachedSearch(retrieval=retrieval, receipt_id=receipt_id, trace=trace)

    def enqueue(self, request: StoreAnswer) -> Job:
        raw = self.client.get(f"{self.root}:receipt:{request.receipt_id}")
        if not isinstance(raw, (str, bytes)):
            raise DomainError("Retrieval receipt missing or expired", 409)
        receipt = Receipt.model_validate_json(raw)
        available = {source.id: source for source in receipt.sources}
        if not set(request.source_ids) <= available.keys():
            raise DomainError("Sources must have been retrieved for this question", 422)
        if sensitive(receipt.query + "\n" + request.answer):
            raise DomainError(
                "Potentially sensitive content cannot enter shared cache", 422
            )
        if not request.answer.strip():
            raise DomainError("Answer must contain text", 422)
        entry = Entry(
            id=uuid4().hex,
            query=receipt.query,
            answer=request.answer,
            catalog=receipt.catalog,
            sources=[available[key] for key in sorted(set(request.source_ids))],
        )
        if not self.current(entry):
            raise DomainError("Sources changed; retrieve policies again", 409)
        entry.id = entry_identity(entry)
        job = Job(id=uuid4().hex, status="queued", entry=entry)
        with self.client.pipeline(transaction=True) as pipeline:
            pipeline.set(f"{self.root}:job:{job.id}", job.model_dump_json(), ex=604800)
            pipeline.xadd(self.queue, {"job_id": job.id})
            pipeline.execute()
        return job

    def job(self, identifier: str) -> Job:
        raw = self.client.get(f"{self.root}:job:{identifier}")
        if not isinstance(raw, (str, bytes)):
            raise DomainError("Cache job not found", 404)
        return Job.model_validate_json(raw)

    def find_duplicate(self, job: Job, blob: bytes) -> Entry | None:
        rows = self.client.execute_command(  # type: ignore[no-untyped-call]
            "FT.SEARCH",
            self.index,
            "*=>[KNN 3 @embedding $v AS distance]",
            "PARAMS",
            2,
            "v",
            blob,
            "SORTBY",
            "distance",
            "RETURN",
            2,
            "payload",
            "distance",
            "LIMIT",
            0,
            3,
            "DIALECT",
            2,
        )
        for offset in range(1, len(rows), 2):
            fields = dict(
                zip(rows[offset + 1][::2], rows[offset + 1][1::2], strict=True)
            )
            if 1 - float(fields["distance"]) < self.similarity:
                continue
            existing = Entry.model_validate_json(fields["payload"])
            # An exact-key write replaces the old admitted answer atomically.
            if existing.id == job.entry.id or not self.current(existing):
                continue
            try:
                verdict = self.judge.decide(
                    {
                        "new_question": job.entry.query,
                        "cached_question": existing.query,
                        "cached_answer": existing.answer,
                    },
                    "reuse",
                )
            except DomainError:
                # Dedup is optional. An unavailable judge must not discard new evidence.
                job.reason = "dedup_unavailable"
                continue
            job.dedup_verdict = verdict
            if (
                verdict.choice == "accept"
                and verdict.confidence >= self.confidence
                and self.current(existing)
                and self.client.exists(rows[offset])
            ):
                return existing
        return None

    def process_one(self) -> bool:
        claimed = self.client.xautoclaim(
            self.queue,
            self.group,
            self.consumer,
            min_idle_time=60000,
            start_id="0-0",
            count=1,
        )
        assert isinstance(claimed, list)  # Synchronous Redis response.
        messages = claimed[1]
        if not messages:
            batches = self.client.xreadgroup(
                self.group, self.consumer, {self.queue: ">"}, count=1
            )
            assert isinstance(batches, list)
            messages = batches[0][1] if batches else []
        if not messages:
            return False
        message_id, fields = messages[0]
        try:
            job = self.job(fields["job_id"])
        except DomainError:
            self.client.xack(self.queue, self.group, message_id)
            return True
        if job.status in ("stored", "duplicate", "rejected", "failed"):
            self.client.xack(self.queue, self.group, message_id)
            return True
        job.entry.id = entry_identity(job.entry)
        job.status = "processing"
        job.attempts += 1
        self.client.set(f"{self.root}:job:{job.id}", job.model_dump_json(), ex=604800)
        try:
            if not self.current(job.entry):
                job.status, job.reason = "rejected", "stale_sources"
            else:
                sources = [
                    self.searcher.policies.get(s.id).markdown for s in job.entry.sources
                ]
                verdict = self.judge.decide(
                    {
                        "question": job.entry.query,
                        "answer": job.entry.answer,
                        "sources": sources,
                    },
                    "admission",
                )
                job.verdict = verdict
                if verdict.choice != "accept" or verdict.confidence < self.confidence:
                    job.status, job.reason = "rejected", "not_public_grounded_guidance"
                else:
                    vectors = self.searcher.embeddings.embed([job.entry.query])
                    if len(vectors) != 1:
                        raise DomainError("Invalid embedding response", 502)
                    blob = self.searcher.vector_bytes(vectors[0])
                    duplicate = self.find_duplicate(job, blob)
                    if not self.current(job.entry):
                        job.status, job.reason = "rejected", "stale_sources"
                    elif duplicate is not None:
                        job.status, job.reason = (
                            "duplicate",
                            "existing_answer_covers_question",
                        )
                        job.duplicate_of = duplicate.id
                    else:
                        key = self.prefix + job.entry.id
                        replaces = bool(self.client.exists(key))
                        with self.client.pipeline(transaction=True) as pipeline:
                            pipeline.hset(
                                key,
                                mapping={
                                    "payload": job.entry.model_dump_json(),
                                    "embedding": blob,
                                },
                            )
                            pipeline.expire(key, 86400)
                            pipeline.execute()
                        job.status = "stored"
                        if replaces:
                            job.reason = "exact_entry_replaced"
        except (DomainError, RedisError, ValueError) as error:
            job.status = "failed" if job.attempts >= 3 else "queued"
            job.reason = "provider_or_storage_failure"
            logger.warning("Cache write failed: %s", type(error).__name__)
        self.client.set(f"{self.root}:job:{job.id}", job.model_dump_json(), ex=604800)
        if job.status != "queued":
            self.client.xack(self.queue, self.group, message_id)
        return True
