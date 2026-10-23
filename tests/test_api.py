import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis import Redis

from moniepoint_demo.api import create_app
from moniepoint_demo.store import RedisWorldStore


@pytest.fixture
def store() -> Iterator[RedisWorldStore]:
    redis = Redis.from_url(
        os.environ.get("TEST_REDIS_URL", "redis://localhost:6387/0"),
        decode_responses=True,
    )
    redis.ping()
    key = f"moniepoint-demo:test:{uuid4()}"
    instance = RedisWorldStore(redis, key)
    yield instance
    for index in redis.execute_command("FT._LIST"):  # type: ignore[no-untyped-call]
        if str(index).startswith(key):
            redis.execute_command("FT.DROPINDEX", index, "DD")  # type: ignore[no-untyped-call]
    keys = list(redis.scan_iter(match=f"{key}*"))
    if keys:
        redis.delete(*keys)
    redis.close()


@pytest.fixture
def client(store: RedisWorldStore) -> Iterator[TestClient]:
    with TestClient(create_app(store)) as http:
        yield http


def test_complete_support_journey_and_return_visit(
    client: TestClient, store: RedisWorldStore
) -> None:
    transaction = client.get("/tools/transactions/txn-001").json()
    assert transaction["status"] == "completed"
    assert client.get("/tools/plan").json()["version"] == 1
    record = client.get("/tools/processing/txn-001").json()
    assert record["reason"] == "transaction_type_excluded"
    assert client.get("/tools/contributions?transaction_id=txn-001").json() == []

    response = client.post(
        "/tools/plan-changes",
        json={
            "conversation_id": "conversation-1",
            "enabled_transaction_types": ["card_payment", "transfer"],
        },
    )
    assert response.status_code == 201
    approval_id = response.json()["id"]
    assert client.post(f"/tools/plan-changes/{approval_id}/apply").status_code == 409
    from moniepoint_demo.simulator import approve_change

    # Seed legacy consent internally; the public presenter bypass is removed.
    store.mutate(lambda world: approve_change(world, approval_id, world.clock))
    assert (
        client.post(f"/tools/plan-changes/{approval_id}/apply").json()["version"] == 2
    )

    response = client.post(
        "/presenter/transactions",
        json={
            "transaction_id": "txn-002",
            "type": "transfer",
            "amount_minor": 1500000,
            "occurred_at": "2026-10-21T10:10:00Z",
            "delivery_delay_seconds": 5,
        },
    )
    assert response.status_code == 201
    assert len(client.get("/tools/contributions?transaction_id=txn-002").json()) == 1
    response = client.post(
        "/presenter/transactions",
        json={
            "transaction_id": "txn-003",
            "type": "transfer",
            "amount_minor": 1500000,
            "occurred_at": "2026-10-21T11:00:00Z",
            "delivery_delay_seconds": 180,
        },
    )
    assert response.status_code == 201
    assert client.get("/tools/processing/txn-003").json()["reason"] == "stale_event"
    assert client.get("/tools/contributions?transaction_id=txn-003").json() == []
    assert client.get("/tools/plan?at=2026-10-21T10:00:00Z").json()["version"] == 1


def test_records_survive_app_restart(store: RedisWorldStore) -> None:
    with TestClient(create_app(store)) as client:
        client.post(
            "/presenter/transactions",
            json={
                "transaction_id": "persist-me",
                "type": "card_payment",
                "amount_minor": 50000,
                "occurred_at": "2026-10-21T10:05:00Z",
            },
        ).raise_for_status()
    with TestClient(create_app(store)) as client:
        assert client.get("/tools/transactions/persist-me").status_code == 200


def test_presenter_reset_restores_initial_fixture(client: TestClient) -> None:
    before = client.get("/presenter/world").json()
    client.post(
        "/presenter/transactions",
        json={
            "transaction_id": "extra",
            "type": "card_payment",
            "amount_minor": 50000,
            "occurred_at": "2026-10-21T10:05:00Z",
        },
    ).raise_for_status()
    assert client.post("/presenter/reset").json() == before


def test_unknown_records_and_invalid_inputs_are_explicit(client: TestClient) -> None:
    assert client.get("/tools/transactions/missing").status_code == 404
    assert client.get("/tools/processing/missing").status_code == 404
    assert client.get("/tools/plan?at=2026-10-21T08:00:00Z").status_code == 404
    assert client.get("/tools/plan?at=2026-10-21T10:00:00").status_code == 422
    assert (
        client.post(
            "/tools/plan-changes",
            json={
                "conversation_id": "conversation-1",
                "enabled_transaction_types": ["bogus"],
            },
        ).status_code
        == 422
    )
    # The model cannot grant approval through a write payload.
    assert (
        client.post(
            "/tools/plan-changes",
            json={
                "conversation_id": "conversation-1",
                "enabled_transaction_types": ["transfer"],
                "approved": True,
            },
        ).status_code
        == 422
    )


def test_transaction_filters_and_no_record_for_failed_payment(
    client: TestClient,
) -> None:
    client.post(
        "/presenter/transactions",
        json={
            "transaction_id": "failed-1",
            "type": "card_payment",
            "amount_minor": 50000,
            "status": "failed",
            "occurred_at": "2026-10-21T10:05:00Z",
        },
    ).raise_for_status()
    matches = client.get("/tools/transactions?status=failed&type=card_payment").json()
    assert [item["transaction_id"] for item in matches] == ["failed-1"]
    assert client.get("/tools/processing/failed-1").json() is None
    assert client.get("/tools/transactions?since=2026-10-21T11:00:00Z").json() == []


def test_customer_catalog_and_distinct_transaction_histories(
    client: TestClient,
) -> None:
    customers = client.get("/presenter/customers")
    assert customers.status_code == 200
    expected_counts = {
        "customer-001": 6,
        "customer-002": 6,
        "customer-003": 6,
        "customer-004": 3,
        "customer-005": 3,
    }
    assert {customer["id"] for customer in customers.json()} == set(expected_counts)
    for customer in customers.json():
        headers = {"X-Demo-Customer-Id": customer["id"]}
        transactions = client.get("/tools/transactions", headers=headers).json()
        assert len(transactions) == expected_counts[customer["id"]]
        assert {tx["customer_id"] for tx in transactions} == {customer["id"]}
        assert (
            client.get("/tools/plan", headers=headers).json()["customer_id"]
            == customer["id"]
        )
        contributions = client.get("/tools/contributions", headers=headers).json()
        assert contributions
        assert {row["customer_id"] for row in contributions} == {customer["id"]}


def test_other_customer_cannot_read_transaction_or_apply_approval(
    client: TestClient,
) -> None:
    other = {"X-Demo-Customer-Id": "customer-002"}
    assert client.get("/tools/transactions/txn-001", headers=other).status_code == 404
    assert client.get("/tools/processing/txn-001", headers=other).status_code == 404
    assert (
        client.get("/tools/contributions?transaction_id=txn-001", headers=other).json()
        == []
    )
    proposal = client.post(
        "/tools/plan-changes",
        json={
            "conversation_id": "ada-chat",
            "enabled_transaction_types": ["transfer"],
        },
    ).json()
    identifier = proposal["id"]
    assert (
        client.post(
            f"/presenter/approvals/{identifier}/confirm", headers=other
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/tools/plan-changes/{identifier}/apply", headers=other
        ).status_code
        == 404
    )


def test_mutation_and_reset_affect_only_selected_customer(client: TestClient) -> None:
    other = {"X-Demo-Customer-Id": "customer-002"}
    ada_before = client.get("/presenter/world").json()
    other_before = client.get("/presenter/world", headers=other).json()
    client.post(
        "/presenter/transactions",
        headers=other,
        json={
            "transaction_id": "new-other",
            "type": "transfer",
            "amount_minor": 100000,
            "occurred_at": "2026-10-21T10:10:00Z",
        },
    ).raise_for_status()
    assert client.get("/presenter/world").json() == ada_before
    assert len(client.get("/tools/transactions", headers=other).json()) == 7
    assert client.post("/presenter/reset", headers=other).json() == other_before
    assert client.get("/presenter/world").json() == ada_before


def test_unknown_customer_does_not_fall_back_to_default(client: TestClient) -> None:
    response = client.get("/tools/plan", headers={"X-Demo-Customer-Id": "unknown"})
    assert response.status_code == 404


def test_each_customer_has_appropriate_savings_amount_and_event_evidence(
    client: TestClient,
) -> None:
    for customer_id, amount in [
        ("customer-001", 50000),
        ("customer-002", 25000),
        ("customer-003", 10000),
    ]:
        headers = {"X-Demo-Customer-Id": customer_id}
        world = client.get("/presenter/world", headers=headers).json()
        assert {row["amount_minor"] for row in world["contributions"]} == {amount}
        assert {row["reason"] for row in world["processing"]} == {
            "contribution_posted",
            "transaction_type_excluded",
            "stale_event",
        }
        incomplete = {
            tx["transaction_id"]
            for tx in world["transactions"]
            if tx["status"] != "completed"
        }
        assert not incomplete.intersection(
            row["transaction_id"] for row in world["processing"]
        )


def test_plan_changes_are_customer_specific(
    client: TestClient, store: RedisWorldStore
) -> None:
    other = {"X-Demo-Customer-Id": "customer-002"}
    before = client.get("/tools/plan", headers=other).json()
    proposal = client.post(
        "/tools/plan-changes",
        json={
            "conversation_id": "ada-chat",
            "enabled_transaction_types": ["transfer"],
        },
    ).json()
    identifier = proposal["id"]
    from moniepoint_demo.simulator import approve_change

    store.mutate(lambda world: approve_change(world, identifier, world.clock))
    client.post(f"/tools/plan-changes/{identifier}/apply").raise_for_status()
    assert client.get("/tools/plan", headers=other).json() == before


def test_adding_fixture_history_preserves_existing_plan_changes(
    store: RedisWorldStore,
) -> None:
    from datetime import UTC, datetime

    from moniepoint_demo.simulator import (
        apply_change,
        approve_change,
        initial_world,
        propose_change,
    )

    world = initial_world()
    at = datetime(2026, 10, 21, 10, 5, tzinfo=UTC)
    proposal = propose_change(world, ["transfer"], "old-chat", at)
    approve_change(world, proposal.id, at)
    apply_change(world, proposal.id, at)
    store.client.set(store.key, world.model_dump_json())
    with TestClient(create_app(store)) as client:
        assert len(client.get("/tools/transactions").json()) == 6
        assert client.get("/tools/plan").json()["version"] == 2
        # Added historical payments still use version 1, not today's version.
        record = client.get("/tools/processing/ada-card-001").json()
        assert record["plan_version_used"] == 1
        assert record["reason"] == "contribution_posted"
    with TestClient(create_app(store)) as client:
        assert len(client.get("/tools/transactions").json()) == 6


def test_policy_catalog_and_markdown_are_available(client: TestClient) -> None:
    response = client.get("/tools/policies")
    assert response.status_code == 200
    catalog = response.json()
    assert {doc["id"] for doc in catalog["documents"]} == {
        "sayt-overview",
        "sayt-create-plan",
        "sayt-manage-plan",
        "sayt-withdrawals",
        "transfers-not-received",
        "transfers-wrong-account",
        "transfers-beneficiaries",
        "cards-lost-stolen",
        "cards-limits",
        "cards-disputes",
        "pos-failed-debit",
        "pos-partial-transfer",
    }
    assert len(catalog["documents"]) == 12
    assert catalog["corpus_version"]
    for summary in catalog["documents"]:
        document = client.get(f"/tools/policies/{summary['id']}")
        assert document.status_code == 200
        body = document.json()
        assert body["markdown"].startswith("# ")
        assert body["source_url"].startswith("https://support.moniepoint.com/")
        assert body["retrieved_on"] == "2026-10-21"
        assert body["document_kind"] == "public_guidance_summary"
        assert body["version"] == summary["version"]
        assert "markdown" not in summary
        raw = client.get(f"/tools/policies/{summary['id']}/markdown")
        assert raw.text == body["markdown"]
        assert raw.headers["content-type"].startswith("text/markdown")
    assert client.get("/tools/policies/not-a-policy").status_code == 404


def test_policy_documents_are_shared_and_survive_customer_reset(
    client: TestClient,
) -> None:
    response = client.get("/tools/policies/sayt-manage-plan")
    response.raise_for_status()
    before = response.json()
    for customer_id in ["customer-001", "customer-002", "customer-003"]:
        headers = {"X-Demo-Customer-Id": customer_id}
        client.post("/presenter/reset", headers=headers).raise_for_status()
        assert (
            client.get("/tools/policies/sayt-manage-plan", headers=headers).json()
            == before
        )


def test_policy_api_reads_persisted_redis_content(store: RedisWorldStore) -> None:
    import json

    with TestClient(create_app(store)) as client:
        key = f"{store.key}:policies"
        raw = store.client.get(key)
        assert isinstance(raw, str)
        corpus = json.loads(raw)
        corpus["documents"][0]["markdown"] = "# Stored document\n\nRead from Redis.\n"
        store.client.set(key, json.dumps(corpus))
        identifier = corpus["documents"][0]["id"]
        assert (
            client.get(f"/tools/policies/{identifier}/markdown").text
            == corpus["documents"][0]["markdown"]
        )


def test_cache_inspection_endpoint(client: TestClient) -> None:
    response = client.get("/presenter/cache?query=card&current=true&limit=3")
    assert response.status_code == 200
    assert response.json() == {"entries": [], "total": 0, "offset": 0, "limit": 3}
    assert client.get("/presenter/cache?limit=101").status_code == 422
    assert client.get("/presenter/cache?offset=-1").status_code == 422


def test_support_tool_catalog_exposes_reviewed_contracts(client: TestClient) -> None:
    response = client.get("/support/tools")
    assert response.status_code == 200
    tools = response.json()
    assert len(tools) == 8
    approval = next(
        tool for tool in tools if tool["name"] == "request_plan_change_approval"
    )
    assert approval["input_schema"]["additionalProperties"] is False
    assert set(approval["input_schema"]["properties"]) == {"proposal_id"}
    assert "conversation can continue" in approval["description"]
