"""Whole-document Redis retrieval with inspectable lexical/vector rank fusion."""

import math
import os
import re
import struct
import time
from hashlib import sha256
from time import perf_counter
from typing import Protocol, cast
from uuid import uuid4

from openai import OpenAI, OpenAIError
from pydantic import Field, field_validator
from redis.exceptions import WatchError

from moniepoint_demo.models import Record
from moniepoint_demo.policies import (
    PolicyCorpus,
    PolicyDocument,
    PolicySource,
    PolicyStore,
)
from moniepoint_demo.simulator import DomainError
from moniepoint_demo.timings import Timings


class Embeddings(Protocol):
    model: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddings:
    model = "text-embedding-3-small"
    dimensions = 1536

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not os.environ.get("OPENAI_API_KEY"):
            raise DomainError("Configure OPENAI_API_KEY to use policy embeddings", 503)
        try:
            with OpenAI(timeout=20, max_retries=1) as client:
                response = client.embeddings.create(
                    model=self.model,
                    input=texts,
                    dimensions=self.dimensions,
                    encoding_format="float",
                )
            return [
                row.embedding for row in sorted(response.data, key=lambda r: r.index)
            ]
        except OpenAIError as error:
            # Provider messages may include request text; don't expose them in the API.
            raise DomainError(
                "Embedding provider unavailable; please retry", 503
            ) from error


class RetrievalRequest(Record):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=3, ge=1, le=3)
    min_similarity: float = Field(
        default=0.25,
        ge=0,
        le=1,
        description="Cosine-similarity floor; uncalibrated, not confidence.",
    )

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Query must contain text")
        return value.strip()


class SearchRequest(RetrievalRequest):
    use_cache: bool = Field(
        default=True,
        description="Enable cache reuse and background nominations. False always "
        "retrieves and generates without modifying existing cached answers.",
    )


class IndexStatus(Record):
    ready: bool
    index_name: str | None = None
    corpus_version: str | None = None
    model: str
    dimensions: int
    document_count: int = 0


class SearchHit(Record):
    document: PolicyDocument
    similarity: float
    vector_rank: int
    lexical_rank: int | None
    fusion_score: float


class SearchResponse(Record):
    timings: Timings = Field(default_factory=Timings)
    query: str
    model: str
    corpus_version: str
    results: list[SearchHit]


def document_text(document: PolicyDocument) -> str:
    """Exclude provenance boilerplate from embeddings; keep it in returned evidence."""
    body = document.markdown.split("\nSource:", 1)[0]
    body = body.split("\nAdditional source:", 1)[0]
    return f"{document.scope}\n\n{body}"


class PolicySearch:
    def __init__(self, policies: PolicyStore, embeddings: Embeddings) -> None:
        self.policies = policies
        self.embeddings = embeddings
        self.state_key = f"{policies.key}:search-state"

    def vector_bytes(self, vector: list[float]) -> bytes:
        if (
            len(vector) != self.embeddings.dimensions
            or not all(math.isfinite(v) for v in vector)
            or not any(vector)
        ):
            raise DomainError("Embedding provider returned an invalid vector", 502)
        return struct.pack(f"<{len(vector)}f", *vector)

    def status(self) -> IndexStatus:
        raw = self.policies.client.get(self.state_key)
        if isinstance(raw, (str, bytes)):
            status = IndexStatus.model_validate_json(raw)
            status.ready = (
                status.corpus_version == self.policies.read().corpus_version
                and status.model == self.embeddings.model
                and status.dimensions == self.embeddings.dimensions
            )
            return status
        return IndexStatus(
            ready=False,
            model=self.embeddings.model,
            dimensions=self.embeddings.dimensions,
        )

    def update_policy(
        self, policy_id: str, expected_version: str, markdown: str
    ) -> IndexStatus:
        original = self.policies.read()
        corpus = original.model_copy(deep=True)
        doc = next((d for d in corpus.documents if d.id == policy_id), None)
        if doc is None:
            raise DomainError("Policy document not found", 404)
        if doc.version != expected_version:
            raise DomainError("Policy changed; reload before editing", 409)
        if not markdown.strip():
            raise DomainError("Policy Markdown must not be empty", 422)
        doc.markdown = markdown
        doc.version = sha256(
            (
                PolicySource.model_validate(
                    doc.model_dump(include=set(PolicySource.model_fields))
                ).model_dump_json()
                + markdown
            ).encode()
        ).hexdigest()
        corpus.corpus_version = sha256(
            "".join(d.version for d in corpus.documents).encode()
        ).hexdigest()
        return self.rebuild(corpus, original.corpus_version)

    def rebuild(
        self, candidate: PolicyCorpus | None = None, expected: str | None = None
    ) -> IndexStatus:
        corpus = candidate or self.policies.read()
        vectors = self.embeddings.embed([document_text(d) for d in corpus.documents])
        if len(vectors) != len(corpus.documents):
            raise DomainError("Embedding provider returned the wrong vector count", 502)
        blobs = [self.vector_bytes(v) for v in vectors]
        index = f"{self.policies.key}:search:{uuid4().hex}"
        prefix = f"{index}:doc:"
        client = self.policies.client
        client.execute_command(  # type: ignore[no-untyped-call]
            "FT.CREATE",
            index,
            "ON",
            "HASH",
            "PREFIX",
            1,
            prefix,
            "SCHEMA",
            "title",
            "TEXT",
            "WEIGHT",
            2,
            "content",
            "TEXT",
            "embedding",
            "VECTOR",
            "FLAT",
            6,
            "TYPE",
            "FLOAT32",
            "DIM",
            self.embeddings.dimensions,
            "DISTANCE_METRIC",
            "COSINE",
        )
        try:
            with client.pipeline(transaction=True) as pipeline:
                for doc, blob in zip(corpus.documents, blobs, strict=True):
                    pipeline.hset(
                        prefix + doc.id,
                        mapping={
                            "title": doc.title,
                            "content": document_text(doc),
                            "document_id": doc.id,
                            "version": doc.version,
                            "embedding": blob,
                        },
                    )
                pipeline.execute()
            deadline = time.monotonic() + 5
            while True:
                count = client.execute_command("FT.SEARCH", index, "*", "LIMIT", 0, 0)[  # type: ignore[no-untyped-call]
                    0
                ]
                if count == len(corpus.documents):
                    break
                if time.monotonic() >= deadline:
                    raise DomainError("Policy index is not ready; retry indexing", 503)
                time.sleep(0.05)
            if self.policies.read().corpus_version != (
                expected or corpus.corpus_version
            ):
                raise DomainError(
                    "Policies changed during indexing; retry indexing", 409
                )
            status = IndexStatus(
                ready=True,
                index_name=index,
                corpus_version=corpus.corpus_version,
                model=self.embeddings.model,
                dimensions=self.embeddings.dimensions,
                document_count=len(corpus.documents),
            )
            # Publish the ready index, document snapshot and cleanup event together.
            with client.pipeline(transaction=True) as pipeline:
                try:
                    pipeline.watch(self.policies.key)  # type: ignore[no-untyped-call]
                    raw = cast(str, pipeline.get(self.policies.key))
                    current = PolicyCorpus.model_validate_json(raw)
                    if current.corpus_version != (expected or corpus.corpus_version):
                        raise DomainError(
                            "Policies changed during indexing; retry", 409
                        )
                    pipeline.multi()
                    if candidate is not None:
                        pipeline.set(self.policies.key, corpus.model_dump_json())
                        pipeline.xadd(
                            f"{self.policies.key}:changes",
                            {"corpus_version": corpus.corpus_version},
                        )
                    pipeline.set(self.state_key, status.model_dump_json())
                    pipeline.execute()
                except WatchError as error:
                    raise DomainError(
                        "Policies changed during indexing; retry", 409
                    ) from error
            return status
        except Exception:
            client.execute_command("FT.DROPINDEX", index, "DD")  # type: ignore[no-untyped-call]
            raise

    def search(self, request: RetrievalRequest) -> SearchResponse:
        started = perf_counter()
        timings = Timings(
            stages_ms=dict.fromkeys(
                [
                    "index_validation",
                    "embedding",
                    "vector_search",
                    "keyword_search",
                    "ranking",
                ],
                0.0,
            )
        )
        result = self._search(request, timings)
        timings.total_ms = (perf_counter() - started) * 1000
        result.timings = timings
        return result

    def _search(self, request: RetrievalRequest, timings: Timings) -> SearchResponse:
        with timings.measure("index_validation"):
            status = self.status()
            if not status.ready or not status.index_name:
                raise DomainError(
                    "Policy search index missing or outdated; rebuild it", 503
                )
            corpus = self.policies.read()
        with timings.measure("embedding"):
            vectors = self.embeddings.embed([request.query])
        if len(vectors) != 1:
            raise DomainError("Embedding provider returned the wrong vector count", 502)
        blob = self.vector_bytes(vectors[0])
        client = self.policies.client
        # Both rankings come from Redis; fuse in Python to expose each rank in Swagger.
        # Exact FLAT search over this small corpus avoids approximate-recall tuning.
        with timings.measure("vector_search"):
            vector_rows = client.execute_command(  # type: ignore[no-untyped-call]
                "FT.SEARCH",
                status.index_name,
                f"*=>[KNN {status.document_count} @embedding $vector AS distance]",
                "PARAMS",
                2,
                "vector",
                blob,
                "SORTBY",
                "distance",
                "RETURN",
                3,
                "document_id",
                "version",
                "distance",
                "LIMIT",
                0,
                status.document_count,
                "DIALECT",
                2,
            )
        with timings.measure("keyword_search"):
            stopwords = {
                "a",
                "an",
                "the",
                "i",
                "my",
                "is",
                "it",
                "to",
                "of",
                "and",
                "or",
                "how",
                "do",
                "can",
                "what",
                "in",
                "for",
                "with",
                "me",
            }
            terms = sorted(
                set(re.findall(r"[a-zA-Z0-9]+", request.query.lower())) - stopwords
            )
            lexical_ranks: dict[str, int] = {}
            if terms:
                rows = client.execute_command(  # type: ignore[no-untyped-call]
                    "FT.SEARCH",
                    status.index_name,
                    " | ".join(terms),
                    "SCORER",
                    "BM25STD",
                    "RETURN",
                    1,
                    "document_id",
                    "LIMIT",
                    0,
                    status.document_count,
                    "DIALECT",
                    2,
                )
                for rank, offset in enumerate(range(1, len(rows), 2), 1):
                    fields = dict(
                        zip(rows[offset + 1][::2], rows[offset + 1][1::2], strict=True)
                    )
                    lexical_ranks[fields["document_id"]] = rank
        with timings.measure("ranking"):
            documents = {doc.id: doc for doc in corpus.documents}
            hits: list[SearchHit] = []
            for rank, offset in enumerate(range(1, len(vector_rows), 2), 1):
                row = vector_rows[offset + 1]
                fields = dict(zip(row[::2], row[1::2], strict=True))
                doc = documents.get(fields["document_id"])
                if doc is None or doc.version != fields["version"]:
                    raise DomainError("Policy search index outdated; rebuild it", 503)
                similarity = max(-1.0, min(1.0, 1 - float(fields["distance"])))
                if similarity < request.min_similarity:
                    continue
                lexical_rank = lexical_ranks.get(doc.id)
                score = 1 / (60 + rank) + (
                    1 / (60 + lexical_rank) if lexical_rank else 0
                )
                hits.append(
                    SearchHit(
                        document=doc,
                        similarity=similarity,
                        vector_rank=rank,
                        lexical_rank=lexical_rank,
                        fusion_score=score,
                    )
                )
            if self.policies.read().corpus_version != status.corpus_version:
                raise DomainError("Policies changed during search; rebuild index", 503)
            hits.sort(
                key=lambda hit: (-hit.fusion_score, hit.vector_rank, hit.document.id)
            )
        return SearchResponse(
            query=request.query,
            model=status.model,
            corpus_version=corpus.corpus_version,
            results=hits[: request.limit],
        )
