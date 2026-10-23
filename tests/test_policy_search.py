import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis import Redis

from moniepoint_demo.api import create_app
from moniepoint_demo.policies import PolicyStore
from moniepoint_demo.policy_search import PolicySearch, SearchRequest
from moniepoint_demo.simulator import DomainError
from moniepoint_demo.store import RedisWorldStore


class TestEmbeddings:
    """Controlled vectors for plumbing tests, not semantic-quality evidence."""

    model = "test-only"
    dimensions = 3

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            [0.0, 0.0, 1.0]
            if text == "astronomy"
            else [1.0, 0.0, 0.0]
            if "lost" in text.lower()
            else [0.0, 1.0, 0.0]
            for text in texts
        ]


@pytest.fixture
def search() -> Iterator[PolicySearch]:
    client = Redis.from_url(
        os.environ.get("TEST_REDIS_URL", "redis://localhost:6387/0"),
        decode_responses=True,
    )
    key = f"test-search:{uuid4().hex}:policies"
    policies = PolicyStore(client, key)
    policies.publish_bundled()
    service = PolicySearch(policies, TestEmbeddings())
    yield service
    for index in client.execute_command("FT._LIST"):  # type: ignore[no-untyped-call]
        if str(index).startswith(key):
            client.execute_command("FT.DROPINDEX", index, "DD")  # type: ignore[no-untyped-call]
    keys = list(client.scan_iter(match=f"{key.removesuffix(':policies')}*"))
    if keys:
        client.delete(*keys)
    client.close()


def test_search_requires_index_and_rejects_changed_corpus(search: PolicySearch) -> None:
    with pytest.raises(DomainError, match="index"):
        search.search(SearchRequest(query="lost card"))
    status = search.rebuild()
    assert status.document_count == 12
    result = search.search(SearchRequest(query="lost card"))
    assert 1 <= len(result.results) <= 3
    assert result.results[0].document.id == "cards-lost-stolen"
    assert result.results[0].document.source_url
    assert result.results[0].document.version
    assert result.results[0].lexical_rank is not None
    corpus = search.policies.read()
    corpus.corpus_version = "changed"
    search.policies.client.set(search.policies.key, corpus.model_dump_json())
    with pytest.raises(DomainError, match="index"):
        search.search(SearchRequest(query="lost card"))


def test_unrelated_vectors_can_return_no_results(search: PolicySearch) -> None:
    search.rebuild()
    assert not search.search(SearchRequest(query="astronomy")).results
    result = search.search(SearchRequest(query="lost", limit=1))
    assert len(result.results) == 1


def test_invalid_queries_and_limits() -> None:
    from pydantic import ValidationError

    for value in ({"query": "   "}, {"query": "x", "limit": 4}):
        with pytest.raises(ValidationError):
            SearchRequest.model_validate(value)


def test_failed_rebuild_preserves_previous_index(search: PolicySearch) -> None:
    previous = search.rebuild()

    class BrokenEmbeddings(TestEmbeddings):
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[float("nan"), 0.0, 0.0] for _ in texts]

    search.embeddings = BrokenEmbeddings()
    with pytest.raises(DomainError, match="invalid vector"):
        search.rebuild()
    assert search.status() == previous
    search.embeddings = TestEmbeddings()
    assert search.search(SearchRequest(query="lost card")).results


def test_search_api_indexing_and_validation(search: PolicySearch) -> None:
    world = RedisWorldStore(
        search.policies.client, search.policies.key.removesuffix(":policies")
    )
    with TestClient(create_app(world, TestEmbeddings())) as client:
        assert client.get("/presenter/policy-index").json()["ready"] is False
        assert (
            client.post(
                "/tools/search-policies", json={"query": "lost card"}
            ).status_code
            == 503
        )
        assert client.post("/presenter/policy-index").json()["document_count"] == 12
        response = client.post("/tools/search-policies", json={"query": "lost card"})
        assert response.status_code == 200
        assert response.json()["results"][0]["document"]["id"] == "cards-lost-stolen"
        assert (
            client.post("/tools/search-policies", json={"query": " "}).status_code
            == 422
        )
        assert (
            client.post(
                "/tools/search-policies", json={"query": "x", "limit": 4}
            ).status_code
            == 422
        )


def test_missing_embedding_credentials_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from moniepoint_demo.policy_search import OpenAIEmbeddings

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(DomainError, match="OPENAI_API_KEY"):
        OpenAIEmbeddings().embed(["hello"])
