from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from moniepoint_demo.models import NewTransaction, World
from moniepoint_demo.simulator import (
    DomainError,
    apply_change,
    approve_change,
    create_transaction,
    get_plan,
    initial_world,
    propose_change,
)

T = datetime(2026, 10, 21, 10, 10, tzinfo=UTC)


def request(
    identifier: str = "txn-new",
    delay: int = 5,
    occurred_at: datetime = T,
    status: str = "completed",
) -> NewTransaction:
    return NewTransaction.model_validate(
        {
            "transaction_id": identifier,
            "type": "transfer",
            "amount_minor": 1500000,
            "occurred_at": occurred_at,
            "delivery_delay_seconds": delay,
            "status": status,
        }
    )


def enabled_world() -> World:
    world = initial_world()
    proposal = propose_change(world, ["card_payment", "transfer"], "conversation-1", T)
    approve_change(world, proposal.id, T)
    apply_change(world, proposal.id, T)
    return world


def test_initial_transfer_was_excluded_and_has_no_contribution() -> None:
    world = initial_world()
    assert world.processing[0].reason == "transaction_type_excluded"
    assert world.processing[0].plan_version_used == 1
    assert world.contributions == []


def test_unapproved_change_cannot_apply() -> None:
    world = initial_world()
    proposal = propose_change(world, ["transfer"], "conversation-1", T)
    with pytest.raises(DomainError, match="approval"):
        apply_change(world, proposal.id, T)
    assert len(world.plans) == 1


def test_approval_creates_new_version_without_rewriting_past() -> None:
    world = enabled_world()
    assert get_plan(world, T - timedelta(seconds=1)).enabled_transaction_types == [
        "card_payment"
    ]
    assert "transfer" in get_plan(world, T).enabled_transaction_types
    assert world.processing[0].reason == "transaction_type_excluded"
    assert world.contributions == []


def test_timely_new_transfer_after_change_posts_once() -> None:
    world = enabled_world()
    tx = request(occurred_at=T + timedelta(seconds=1))
    create_transaction(world, tx)
    create_transaction(world, tx)
    assert len(world.contributions) == 1
    assert world.contributions[0].amount_minor == 50000
    assert world.contributions[0].transaction_id == tx.transaction_id
    assert world.processing[-1].status == "processed"


def test_same_symptom_different_cause_after_plan_change() -> None:
    world = enabled_world()
    create_transaction(world, request(delay=180, occurred_at=T + timedelta(minutes=1)))
    assert world.processing[-1].reason == "stale_event"
    assert world.processing[-1].plan_version_used == 2
    assert world.contributions == []


@pytest.mark.parametrize(("delay", "expected"), [(120, "processed"), (121, "dropped")])
def test_freshness_boundary(delay: int, expected: str) -> None:
    world = enabled_world()
    create_transaction(world, request(delay=delay))
    assert world.processing[-1].status == expected


def test_delayed_delivery_uses_plan_at_transaction_time() -> None:
    world = enabled_world()
    create_transaction(world, request(occurred_at=T - timedelta(seconds=1)))
    assert world.processing[-1].plan_version_used == 1
    assert world.processing[-1].reason == "transaction_type_excluded"


def test_conflicting_transaction_replay_is_rejected() -> None:
    world = initial_world()
    create_transaction(world, request())
    changed = request().model_copy(update={"amount_minor": 12345})
    with pytest.raises(DomainError, match="different"):
        create_transaction(world, changed)


def test_stale_approval_cannot_overwrite_newer_plan() -> None:
    world = initial_world()
    first = propose_change(world, ["transfer"], "conversation-1", T)
    second = propose_change(world, ["card_payment", "transfer"], "conversation-2", T)
    approve_change(world, first.id, T)
    approve_change(world, second.id, T)
    apply_change(world, second.id, T)
    with pytest.raises(DomainError, match="changed"):
        apply_change(world, first.id, T + timedelta(seconds=1))


def test_reapplying_approved_change_is_idempotent() -> None:
    world = enabled_world()
    plan = apply_change(world, world.approvals[0].id, T + timedelta(seconds=1))
    assert plan.version == 2
    assert len(world.plans) == 2


@pytest.mark.parametrize("status", ["failed", "pending"])
def test_noncompleted_transactions_cannot_create_savings(status: str) -> None:
    world = enabled_world()
    create_transaction(world, request(status=status))
    assert world.processing == initial_world().processing
    assert world.contributions == []


def test_unknown_approval_is_explicit_error() -> None:
    with pytest.raises(DomainError, match="not found"):
        approve_change(initial_world(), "missing", T)


def test_negative_amount_delay_and_naive_time_are_rejected() -> None:
    updates: list[dict[str, object]] = [
        {"amount_minor": -1},
        {"delivery_delay_seconds": -1},
        {"occurred_at": datetime(2026, 10, 21)},
    ]
    for update in updates:
        with pytest.raises(ValidationError):
            NewTransaction.model_validate(request().model_dump() | update)


def test_reset_builds_independent_identical_worlds() -> None:
    first = initial_world()
    second = initial_world()
    create_transaction(first, request())
    assert second == initial_world()
    assert len(first.transactions) == len(second.transactions) + 1
