from collections.abc import Iterator
from typing import Literal
from uuid import uuid4

import pytest
from redis import Redis

from moniepoint_demo.jev import Verdict
from moniepoint_demo.policies import PolicyStore
from moniepoint_demo.policy_cache import PolicyCache, StoreAnswer
from moniepoint_demo.policy_search import PolicySearch, SearchRequest
from moniepoint_demo.simulator import DomainError


class Embeddings:
    model = "test-cache"
    dimensions = 3

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


class Judge:
    choice: Literal["accept", "reject", "uncertain"] = "accept"
    confidence = 0.9

    def decide(self, state: dict[str, object], purpose: str) -> Verdict:
        return Verdict(
            choice=self.choice, confidence=self.confidence, model="test-judge"
        )


@pytest.fixture
def cache() -> Iterator[PolicyCache]:
    client = Redis.from_url("redis://localhost:6387/0", decode_responses=True)
    key = f"test-cache:{uuid4().hex}"
    policies = PolicyStore(client, key)
    policies.publish_bundled()
    search = PolicySearch(policies, Embeddings())
    search.rebuild()
    instance = PolicyCache(search, Judge())
    instance.initialize()
    yield instance
    for index in client.execute_command("FT._LIST"):  # type: ignore[no-untyped-call]
        if str(index).startswith(key):
            client.execute_command("FT.DROPINDEX", index, "DD")  # type: ignore[no-untyped-call]
    keys = list(client.scan_iter(match=f"{key}*"))
    if keys:
        client.delete(*keys)
    client.close()


def nominate(cache: PolicyCache) -> str:
    found = cache.search(SearchRequest(query="How can I change my card limits?"))
    assert found.retrieval is not None and found.receipt_id
    source = found.retrieval.results[0].document.id
    job = cache.enqueue(
        StoreAnswer(
            receipt_id=found.receipt_id,
            answer="Open Cards and change the limit in settings.",
            source_ids=[source],
        )
    )
    assert job.status == "queued"
    return job.id


def test_queue_hit_and_jev_rejection(cache: PolicyCache) -> None:
    job = nominate(cache)
    assert cache.process_one()
    assert cache.job(job).status == "stored"
    hit = cache.search(SearchRequest(query="How can I change my card limits?"))
    assert hit.answer and hit.retrieval is None
    cache.judge = RejectJudge()
    rejected = cache.search(
        SearchRequest(query="Change it and tell me the maximum allowed")
    )
    assert rejected.answer is None and rejected.retrieval is not None
    assert any(step.reason == "jev_rejected" for step in rejected.trace)


class RejectJudge(Judge):
    choice = "reject"


def test_no_fabricated_sources_and_no_personal_cache(cache: PolicyCache) -> None:
    result = cache.search(SearchRequest(query="How can I change my card limits?"))
    assert result.receipt_id and result.retrieval
    with pytest.raises(DomainError, match="retrieved"):
        cache.enqueue(
            StoreAnswer(
                receipt_id=result.receipt_id, answer="Example", source_ids=["invented"]
            )
        )
    with pytest.raises(DomainError, match="sensitive"):
        cache.enqueue(
            StoreAnswer(
                receipt_id=result.receipt_id,
                answer="Email ada@example.com",
                source_ids=[result.retrieval.results[0].document.id],
            )
        )


def test_sources_rechecked_before_store_and_reuse(cache: PolicyCache) -> None:
    job = nominate(cache)
    corpus = cache.searcher.policies.read()
    for doc in corpus.documents:
        doc.version = "changed"
    cache.client.set(cache.searcher.policies.key, corpus.model_dump_json())
    cache.process_one()
    assert cache.job(job).status == "rejected"


def test_low_confidence_falls_back(cache: PolicyCache) -> None:
    nominate(cache)
    cache.process_one()

    class UncertainJudge(Judge):
        confidence = 0.1

    cache.judge = UncertainJudge()
    result = cache.search(SearchRequest(query="How can I change my card limits?"))
    assert result.answer is None and result.retrieval is not None


def test_stale_hit_and_new_documents_are_not_reused(cache: PolicyCache) -> None:
    nominate(cache)
    cache.process_one()
    corpus = cache.searcher.policies.read()
    added = corpus.documents[0].model_copy(update={"id": "new-policy"})
    corpus.documents.append(added)
    corpus.corpus_version = "new-corpus"
    cache.client.set(cache.searcher.policies.key, corpus.model_dump_json())
    cache.searcher.rebuild()
    result = cache.search(SearchRequest(query="How can I change my card limits?"))
    assert result.answer is None
    assert any(step.reason == "stale_sources" for step in result.trace)


def test_unrelated_edit_keeps_cached_answer(cache: PolicyCache) -> None:
    job_id = nominate(cache)
    cache.process_one()
    job = cache.job(job_id)
    corpus = cache.searcher.policies.read()
    unrelated = next(d for d in corpus.documents if d.id != job.entry.sources[0].id)
    unrelated.version = "unrelated-edit"
    corpus.corpus_version = "another-corpus"
    cache.client.set(cache.searcher.policies.key, corpus.model_dump_json())
    assert cache.search(SearchRequest(query="How can I change my card limits?")).answer


def test_verifier_error_falls_back(cache: PolicyCache) -> None:
    nominate(cache)
    cache.process_one()

    class FailedJudge(Judge):
        def decide(self, state: dict[str, object], purpose: str) -> Verdict:
            raise DomainError("Provider timeout", 503)

    cache.judge = FailedJudge()
    result = cache.search(SearchRequest(query="How can I change my card limits?"))
    assert result.answer is None and result.retrieval
    assert result.trace[-1].reason == "cache_or_verifier_unavailable"


def test_invalid_jev_response_is_rejected() -> None:
    from pydantic import ValidationError

    from moniepoint_demo.jev import Choice

    with pytest.raises(ValidationError):
        Choice.model_validate(
            {
                "type": "choice",
                "choice": "accept",
                "confidence": 0.99,
                "probabilities": {"accept": 0.1, "reject": 0.8, "uncertain": 0.1},
            }
        )


def test_failed_jobs_retry_and_stop_after_three_attempts(cache: PolicyCache) -> None:
    job_id = nominate(cache)

    class FailedJudge(Judge):
        def decide(self, state: dict[str, object], purpose: str) -> Verdict:
            raise DomainError("Provider offline", 503)

    cache.judge = FailedJudge()
    for attempt in range(1, 4):
        assert cache.process_one()
        job = cache.job(job_id)
        assert job.attempts == attempt
        if attempt < 3:
            assert job.status == "queued"
            pending = cache.client.xpending_range(cache.queue, cache.group, "-", "+", 1)
            assert isinstance(pending, list)
            cache.client.xclaim(
                cache.queue,
                cache.group,
                cache.consumer,
                0,
                [pending[0]["message_id"]],
                idle=60001,
            )
    assert cache.job(job_id).status == "failed"
    pending_summary = cache.client.xpending(cache.queue, cache.group)
    assert isinstance(pending_summary, dict)
    assert pending_summary["pending"] == 0


def test_cache_toggle_bypasses_lookup_and_jev_without_deleting_entry(
    cache: PolicyCache,
) -> None:
    job_id = nominate(cache)
    cache.process_one()
    payload = cache.client.hget(cache.prefix + cache.job(job_id).entry.id, "payload")
    original_index, original_judge = cache.index, cache.judge

    class MustNotRun(Judge):
        def decide(self, state: dict[str, object], purpose: str) -> Verdict:
            raise AssertionError("Jev must not run with caching disabled")

    cache.index = "nonexistent-cache-index"
    cache.judge = MustNotRun()
    result = cache.search(
        SearchRequest(query="How can I change my card limits?", use_cache=False)
    )
    assert result.answer is None and result.retrieval is not None
    assert result.receipt_id
    assert [step.reason for step in result.trace] == ["cache_bypassed"]
    assert (
        cache.client.hget(cache.prefix + cache.job(job_id).entry.id, "payload")
        == payload
    )

    cache.index, cache.judge = original_index, original_judge
    enabled = cache.search(
        SearchRequest(query="How can I change my card limits?", use_cache=True)
    )
    assert enabled.answer
    assert enabled.trace[0].reason == "cache_hit"


def test_search_timings_distinguish_hit_and_bypass(cache: PolicyCache) -> None:
    nominate(cache)
    cache.process_one()
    hit = cache.search(SearchRequest(query="How can I change my card limits?"))
    assert hit.timings.total_ms > 0
    assert hit.timings.stages_ms["jev"] > 0
    assert hit.timings.stages_ms["retrieval"] == 0
    bypass = cache.search(
        SearchRequest(query="How can I change my card limits?", use_cache=False)
    )
    assert bypass.timings.stages_ms["cache_embedding"] == 0
    assert bypass.timings.stages_ms["cache_lookup"] == 0
    assert bypass.timings.stages_ms["jev"] == 0
    assert bypass.timings.stages_ms["retrieval"] > 0
    assert bypass.retrieval is not None
    assert bypass.retrieval.timings.stages_ms["embedding"] > 0
    assert bypass.retrieval.timings.stages_ms["vector_search"] > 0
    assert bypass.retrieval.timings.stages_ms["keyword_search"] > 0
    assert bypass.timings.total_ms >= sum(bypass.timings.stages_ms.values())


def nominate_question(cache: PolicyCache, question: str, answer: str) -> str:
    result = cache.search(SearchRequest(query=question, use_cache=False))
    assert result.receipt_id and result.retrieval
    job = cache.enqueue(
        StoreAnswer(
            receipt_id=result.receipt_id,
            answer=answer,
            source_ids=[result.retrieval.results[0].document.id],
        )
    )
    cache.process_one()
    return job.id


def test_semantic_duplicate_keeps_existing_answer(cache: PolicyCache) -> None:
    first = nominate_question(
        cache, "How do I change card limits?", "Open card settings."
    )
    second = nominate_question(
        cache, "How can I adjust card limits?", "Use card settings."
    )
    job = cache.job(second)
    assert job.status == "duplicate"
    assert job.duplicate_of == cache.job(first).entry.id
    assert job.dedup_verdict and job.dedup_verdict.choice == "accept"
    assert len(list(cache.client.scan_iter(match=cache.prefix + "*"))) == 1


@pytest.mark.parametrize(
    "decision,confidence", [("reject", 0.9), ("uncertain", 0.9), ("accept", 0.1)]
)
def test_semantic_dedup_preserves_uncertain_or_different_answers(
    cache: PolicyCache,
    decision: Literal["accept", "reject", "uncertain"],
    confidence: float,
) -> None:
    nominate_question(cache, "How do I change card limits?", "Open card settings.")

    class DedupJudge(Judge):
        def decide(self, state: dict[str, object], purpose: str) -> Verdict:
            if purpose == "reuse":
                return Verdict(choice=decision, confidence=confidence, model="test")
            return super().decide(state, purpose)

    cache.judge = DedupJudge()
    second = nominate_question(
        cache, "How can I adjust card limits?", "Use card settings."
    )
    assert cache.job(second).status == "stored"
    assert len(list(cache.client.scan_iter(match=cache.prefix + "*"))) == 2


def test_exact_repeat_has_one_entry_even_when_jev_is_uncertain(
    cache: PolicyCache,
) -> None:
    # Hold the retrieval receipt fixed: this tests identical source snapshots,
    # not ranking stability when the fixture gives every document the same vector.
    found = cache.search(
        SearchRequest(query="How do I change card limits?", use_cache=False)
    )
    assert found.receipt_id and found.retrieval
    request = StoreAnswer(
        receipt_id=found.receipt_id,
        answer="Open card settings.",
        source_ids=[found.retrieval.results[0].document.id],
    )
    first = cache.enqueue(request).id
    cache.process_one()

    class Uncertain(Judge):
        def decide(self, state: dict[str, object], purpose: str) -> Verdict:
            if purpose == "reuse":
                return Verdict(choice="uncertain", confidence=0.3, model="test")
            return super().decide(state, purpose)

    cache.judge = Uncertain()
    second = cache.enqueue(
        request.model_copy(update={"answer": "Use card settings."})
    ).id
    cache.process_one()
    assert first != second
    assert cache.job(first).entry.id == cache.job(second).entry.id
    assert len(list(cache.client.scan_iter(match=cache.prefix + "*"))) == 1


def test_inspect_cache_filters_and_freshness(cache: PolicyCache) -> None:
    assert cache.list_entries().total == 0
    job_id = nominate(cache)
    cache.process_one()
    entry = cache.job(job_id).entry
    page = cache.list_entries(query="CARDS", source_id=entry.sources[0].id)
    assert page.total == 1
    assert page.entries[0].id == entry.id
    assert page.entries[0].current
    assert 0 < page.entries[0].ttl_seconds <= 86400
    assert cache.list_entries(query="unmatched").total == 0
    assert cache.list_entries(source_id="missing").total == 0
    assert cache.list_entries(offset=1).entries == []
    corpus = cache.searcher.policies.read()
    for doc in corpus.documents:
        doc.version = "updated"
    cache.client.set(cache.searcher.policies.key, corpus.model_dump_json())
    assert cache.list_entries(current=True).total == 0
    stale = cache.list_entries(current=False)
    assert stale.total == 1
    assert not stale.entries[0].current


def test_policy_publication_and_cleanup(cache: PolicyCache) -> None:
    job_id = nominate(cache)
    cache.process_one()
    entry = cache.job(job_id).entry
    source = cache.searcher.policies.get(entry.sources[0].id)
    unrelated = next(
        d for d in cache.searcher.policies.read().documents if d.id != source.id
    )
    # A second entry with independent provenance must survive the edit.
    other = entry.model_copy(
        update={
            "id": "unrelated",
            "sources": [
                type(entry.sources[0])(id=unrelated.id, version=unrelated.version)
            ],
        }
    )
    cache.client.hset(
        cache.prefix + other.id, mapping={"payload": other.model_dump_json()}
    )
    cache.searcher.update_policy(
        source.id, source.version, source.markdown + "\nUpdated test guidance."
    )
    assert cache.searcher.status().ready
    assert not cache.current(entry)
    assert cache.current(other)
    assert cache.client.exists(cache.prefix + entry.id)
    assert cache.process_invalidation() == 1
    assert not cache.client.exists(cache.prefix + entry.id)
    assert cache.client.exists(cache.prefix + other.id)
    assert cache.process_invalidation() == 0
    with pytest.raises(DomainError):
        cache.searcher.update_policy(source.id, source.version, "Outdated edit")


def test_failed_publication_keeps_current_policy_and_index(cache: PolicyCache) -> None:
    original = cache.searcher.policies.read()
    index = cache.searcher.status().index_name

    class BrokenEmbeddings(Embeddings):
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("provider unavailable")

    cache.searcher.embeddings = BrokenEmbeddings()
    doc = original.documents[0]
    with pytest.raises(RuntimeError):
        cache.searcher.update_policy(doc.id, doc.version, "New text")
    assert cache.searcher.policies.read() == original
    assert cache.searcher.status().index_name == index
    assert cache.searcher.status().ready
