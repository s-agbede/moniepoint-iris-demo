from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis import Redis

from moniepoint_demo.api import create_app
from moniepoint_demo.chat import GeneratedAnswer
from moniepoint_demo.jev import Verdict
from moniepoint_demo.policies import PolicyDocument, PolicyStore
from moniepoint_demo.policy_cache import PolicyCache
from moniepoint_demo.policy_search import PolicySearch
from moniepoint_demo.store import RedisWorldStore


class Embeddings:
    model = "chat-test"
    dimensions = 3

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in texts]


class Judge:
    def decide(self, state: dict[str, object], purpose: str) -> Verdict:
        return Verdict(choice="accept", confidence=0.9, model="test")


class Generator:
    model = "test-generator"
    calls = 0

    def generate(
        self, question: str, documents: list[PolicyDocument]
    ) -> GeneratedAnswer:
        self.calls += 1
        return GeneratedAnswer(
            answer="Open Card Settings and select Card Limits.",
            source_ids=[documents[0].id],
            cacheable=True,
        )


@pytest.fixture
def setup() -> Iterator[tuple[TestClient, PolicyCache, Generator]]:
    client = Redis.from_url("redis://localhost:6387/0", decode_responses=True)
    key = f"chat-test:{uuid4().hex}"
    world = RedisWorldStore(client, key)
    generator = Generator()
    with TestClient(create_app(world, Embeddings(), Judge(), generator)) as http:
        assert http.post("/presenter/policy-index").status_code == 200
        cache = PolicyCache(
            PolicySearch(PolicyStore(client, key + ":policies"), Embeddings()), Judge()
        )
        yield http, cache, generator
    for index in client.execute_command("FT._LIST"):  # type: ignore[no-untyped-call]
        if str(index).startswith(key):
            client.execute_command("FT.DROPINDEX", index, "DD")  # type: ignore[no-untyped-call]
    keys = list(client.scan_iter(match=key + "*"))
    if keys:
        client.delete(*keys)
    client.close()


def test_chat_generates_on_miss_reuses_hit_and_bypasses(
    setup: tuple[TestClient, PolicyCache, Generator],
) -> None:
    http, cache, generator = setup
    question = {"query": "How do I change my card limit?"}
    first = http.post("/chat", json=question)
    assert first.status_code == 200
    result = first.json()
    assert result["answer"] and result["answer_source"] == "generated"
    assert result["timings"]["stages_ms"]["generation"] > 0
    assert result["cache_job_id"]
    cache.process_one()
    assert cache.job(result["cache_job_id"]).status == "stored"
    hit = http.post("/chat", json=question).json()
    assert hit["answer_source"] == "cache"
    assert hit["timings"]["stages_ms"]["generation"] == 0
    assert generator.calls == 1
    bypass = http.post("/chat", json={**question, "use_cache": False}).json()
    assert bypass["answer_source"] == "generated"
    assert bypass["cache_job_id"] is None
    assert generator.calls == 2
    assert bypass["trace"][0]["reason"] == "cache_bypassed"
    assert bypass["timings"]["total_ms"] >= sum(bypass["timings"]["stages_ms"].values())
    assert bypass["sources"][0]["source_url"]
    assert http.post("/chat", json=question).json()["answer_source"] == "cache"


def test_policy_search_is_retrieval_only(
    setup: tuple[TestClient, PolicyCache, Generator],
) -> None:
    http, _, generator = setup
    result = http.post("/tools/search-policies", json={"query": "card limits"}).json()
    assert result["results"]
    assert "answer" not in result
    assert generator.calls == 0


def test_generation_failure_is_not_reported_as_an_answer(
    setup: tuple[TestClient, PolicyCache, Generator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from moniepoint_demo.simulator import DomainError

    http, _, generator = setup

    def fail(question: str, documents: list[PolicyDocument]) -> GeneratedAnswer:
        raise DomainError("Answer generation unavailable; please retry", 503)

    monkeypatch.setattr(generator, "generate", fail)
    response = http.post("/chat", json={"query": "card limits", "use_cache": False})
    assert response.status_code == 503
    assert "answer" not in response.json()


def test_generated_citations_must_come_from_retrieval(
    setup: tuple[TestClient, PolicyCache, Generator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http, _, generator = setup

    def invent_source(
        question: str, documents: list[PolicyDocument]
    ) -> GeneratedAnswer:
        return GeneratedAnswer(
            answer="An unsupported assertion.",
            source_ids=["invented-policy"],
            cacheable=True,
        )

    monkeypatch.setattr(generator, "generate", invent_source)
    response = http.post("/chat", json={"query": "card limits", "use_cache": False})
    assert response.status_code == 502
    assert "did not retrieve" in response.json()["detail"]
