from collections.abc import Iterator
from uuid import uuid4

import pytest
from redis import Redis

from moniepoint_demo import simulator
from moniepoint_demo.simulator import DomainError
from moniepoint_demo.store import RedisWorldStore


@pytest.fixture
def store() -> Iterator[RedisWorldStore]:
    client = Redis.from_url("redis://localhost:6387/0", decode_responses=True)
    key = f"test-support:{uuid4().hex}"
    instance = RedisWorldStore(client, key)
    instance.initialize()
    yield instance
    keys = list(client.scan_iter(match=f"{key}*"))
    if keys:
        client.delete(*keys)
    client.close()


def test_session_bound_proposal_and_legacy_bypass(store: RedisWorldStore) -> None:
    from moniepoint_demo.support_service import SupportService

    service = SupportService(store)
    session = service.create_session(use_cache=False)
    proposal = service.propose(session.id, ["card_payment", "transfer"], "call-1")
    assert proposal.before_enabled_transaction_types == ["card_payment"]
    assert proposal.conversation_id == session.id
    assert (
        service.propose(session.id, ["transfer", "card_payment"], "call-1").id
        == proposal.id
    )
    with pytest.raises(DomainError, match="different"):
        service.propose(session.id, ["transfer"], "call-1")
    with pytest.raises(DomainError, match="session"):
        store.mutate(
            lambda world: simulator.approve_change(world, proposal.id, world.clock)
        )
    with pytest.raises(DomainError, match="approval"):
        service.apply(session.id, proposal.id)
    service.request_approval(session.id, proposal.id)
    service.decide(session.id, proposal.id, "approve", "decision-1")
    with pytest.raises(DomainError, match="session"):
        store.mutate(
            lambda world: simulator.apply_change(world, proposal.id, world.clock)
        )
    applied = service.apply(session.id, proposal.id)
    assert applied.enabled_transaction_types == ["card_payment", "transfer"]
    assert service.apply(session.id, proposal.id).version == applied.version
    assert (
        service.decide(session.id, proposal.id, "approve", "decision-1").status
        == "applied"
    )
    with pytest.raises(DomainError):
        service.decide(session.id, proposal.id, "decline", "decision-2")


def test_replacement_decline_ownership_and_reset(store: RedisWorldStore) -> None:
    from moniepoint_demo.support_service import SupportService

    service = SupportService(store)
    first = service.create_session()
    other = service.create_session()
    proposal = service.propose(first.id, ["transfer"], "call-1")
    with pytest.raises(DomainError, match="not found"):
        service.request_approval(other.id, proposal.id)
    replacement = service.propose(first.id, [], "call-2")
    with pytest.raises(DomainError, match="active"):
        service.decide(first.id, proposal.id, "approve", "old-card")
    assert (
        service.request_approval(first.id, replacement.id).enabled_transaction_types
        == []
    )
    assert (
        service.decide(first.id, replacement.id, "decline", "decision").status
        == "declined"
    )
    with pytest.raises(DomainError):
        service.apply(first.id, replacement.id)
    assert len(store.read().plans) == 1
    store.reset()
    with pytest.raises(DomainError, match="Session not found"):
        service.session(first.id)


def test_stale_proposal_and_cross_customer(store: RedisWorldStore) -> None:
    from moniepoint_demo.support_service import SupportService

    service = SupportService(store)
    session = service.create_session()
    proposal = service.propose(session.id, ["transfer"], "call-1")
    second_store = store.for_customer("customer-002")
    second_store.initialize()
    with pytest.raises(DomainError, match="Session not found"):
        SupportService(second_store).session(session.id)
    service.request_approval(session.id, proposal.id)
    other = service.create_session()
    other_proposal = service.propose(other.id, ["airtime"], "call-2")
    service.request_approval(other.id, other_proposal.id)
    service.decide(other.id, other_proposal.id, "approve", "d2")
    service.apply(other.id, other_proposal.id)
    with pytest.raises(DomainError, match="changed"):
        service.decide(session.id, proposal.id, "approve", "d1")


def test_tool_contract_rejects_model_identity_and_unknown_transaction(
    store: RedisWorldStore,
) -> None:
    from pydantic import ValidationError

    from moniepoint_demo.support_service import SupportService
    from moniepoint_demo.support_tools import SupportTools, tool_catalog

    service = SupportService(store)
    session = service.create_session()
    tools = SupportTools(service, session.id)
    catalog = tool_catalog()
    assert len(catalog) == 8
    for tool in catalog:
        assert "WHEN NOT TO USE:" in tool.description
        assert "DO NOT USE FOR:" in tool.description
        assert "EXAMPLES:" in tool.description
        assert "customer_id" not in tool.input_schema["properties"]
        assert "session_id" not in tool.input_schema["properties"]
    with pytest.raises(ValidationError):
        tools.execute("get_savings_plan", {"customer_id": "customer-002"}, "call-1")
    with pytest.raises(DomainError, match="Transaction not found"):
        tools.execute(
            "get_savings_contributions", {"transaction_id": "missing"}, "call-2"
        )
    result = tools.execute("get_savings_plan", {}, "call-3")
    assert result["customer_id"] == "customer-001"


def test_decision_requires_card_and_application_rechecks_plan(
    store: RedisWorldStore,
) -> None:
    from moniepoint_demo.support_service import SupportService

    service = SupportService(store)
    session = service.create_session()
    with pytest.raises(DomainError, match="no change"):
        service.propose(session.id, ["card_payment"], "noop")
    proposal = service.propose(session.id, ["transfer"], "call-1")
    with pytest.raises(DomainError, match="presented"):
        service.decide(session.id, proposal.id, "approve", "d1")
    service.request_approval(session.id, proposal.id)
    service.decide(session.id, proposal.id, "approve", "d1")
    with pytest.raises(DomainError, match="finish"):
        service.propose(session.id, ["airtime"], "call-2")
    other = service.create_session()
    replacement = service.propose(other.id, ["bill_payment"], "call-3")
    service.request_approval(other.id, replacement.id)
    service.decide(other.id, replacement.id, "approve", "d2")
    service.apply(other.id, replacement.id)
    with pytest.raises(DomainError, match="changed"):
        service.apply(session.id, proposal.id)


def test_reads_work_while_approval_pending(store: RedisWorldStore) -> None:
    from moniepoint_demo.support_service import SupportService
    from moniepoint_demo.support_tools import SupportTools

    service = SupportService(store)
    session = service.create_session()
    tools = SupportTools(service, session.id)
    proposal = tools.execute(
        "propose_savings_plan_change",
        {"enabled_transaction_types": ["transfer"]},
        "call-1",
    )
    tools.execute(
        "request_plan_change_approval", {"proposal_id": proposal["id"]}, "call-2"
    )
    tx = tools.execute("get_transaction", {"transaction_id": "txn-001"}, "call-3")
    plan = tools.execute("get_savings_plan", {"at": tx["occurred_at"]}, "call-4")
    assert plan["enabled_transaction_types"] == ["card_payment"]
    processing = tools.execute(
        "get_sayt_processing", {"transaction_id": "txn-001"}, "call-5"
    )
    assert processing["processing"]["reason"] == "transaction_type_excluded"
    assert tools.execute(
        "get_savings_contributions", {"transaction_id": "txn-001"}, "call-6"
    ) == {"contributions": []}
    assert store.read().approvals[-1].status == "pending"


def test_stale_approved_proposal_can_be_replaced(store: RedisWorldStore) -> None:
    from moniepoint_demo.support_service import SupportService

    service = SupportService(store)
    first = service.create_session()
    proposal = service.propose(first.id, ["transfer"], "p1")
    service.request_approval(first.id, proposal.id)
    service.decide(first.id, proposal.id, "approve", "d1")
    other = service.create_session()
    competing = service.propose(other.id, ["airtime"], "p2")
    service.request_approval(other.id, competing.id)
    service.decide(other.id, competing.id, "approve", "d2")
    service.apply(other.id, competing.id)
    with pytest.raises(DomainError, match="changed"):
        service.apply(first.id, proposal.id)
    replacement = service.propose(first.id, ["transfer", "airtime"], "p3")
    assert replacement.status == "pending"
    assert replacement.base_plan_version == 2
