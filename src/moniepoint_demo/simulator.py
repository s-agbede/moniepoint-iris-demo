"""State transitions. No dialogue, agent routing, or scenario-specific diagnoses."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from moniepoint_demo.models import (
    Approval,
    Contribution,
    Customer,
    NewTransaction,
    ProcessingRecord,
    SavingsPlan,
    Transaction,
    TransactionType,
    World,
)


class DomainError(Exception):
    def __init__(self, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


def get_plan(world: World, at: datetime | None = None) -> SavingsPlan:
    instant = at or world.clock
    eligible = [plan for plan in world.plans if plan.effective_from <= instant]
    if not eligible:
        raise DomainError("No plan existed at that time", 404)
    return max(eligible, key=lambda plan: (plan.effective_from, plan.version))


def create_transaction(world: World, request: NewTransaction) -> Transaction:
    existing = next(
        (
            tx
            for tx in world.transactions
            if tx.transaction_id == request.transaction_id
        ),
        None,
    )
    if existing:
        if existing.model_dump(exclude={"customer_id"}) != request.model_dump():
            raise DomainError("Transaction ID already exists with different content")
        return existing

    transaction = Transaction(**request.model_dump(), customer_id=world.customer.id)
    # Resolve eligibility before mutating, so an invalid historical request is atomic.
    plan = get_plan(world, request.occurred_at)
    world.transactions.append(transaction)
    world.advance_to(request.occurred_at)
    if request.status != "completed":
        return transaction

    received_at = request.occurred_at + timedelta(
        seconds=request.delivery_delay_seconds
    )
    event_id = f"event-{request.transaction_id}"
    status = "processed"
    reason = "contribution_posted"
    if request.delivery_delay_seconds > world.freshness_window_seconds:
        status, reason = "dropped", "stale_event"
    elif request.type not in plan.enabled_transaction_types:
        status, reason = "excluded", "transaction_type_excluded"
    else:
        world.contributions.append(
            Contribution(
                id=f"contribution-{request.transaction_id}",
                customer_id=world.customer.id,
                plan_id=plan.id,
                transaction_id=request.transaction_id,
                event_id=event_id,
                amount_minor=plan.fixed_contribution_minor,
                posted_at=received_at,
            )
        )
    world.processing.append(
        ProcessingRecord.model_validate(
            {
                "event_id": event_id,
                "transaction_id": request.transaction_id,
                "plan_version_used": plan.version,
                "produced_at": request.occurred_at,
                "received_at": received_at,
                "status": status,
                "reason": reason,
            }
        )
    )
    # Simulated time advances immediately; there is no wall-clock sleep or scheduler.
    world.advance_to(received_at)
    return transaction


def propose_change(
    world: World,
    enabled_types: list[TransactionType],
    conversation_id: str,
    at: datetime,
) -> Approval:
    if at < world.clock:
        raise DomainError("Cannot propose a change in the past")
    plan = get_plan(world, at)
    proposal = Approval(
        id=str(uuid4()),
        customer_id=world.customer.id,
        conversation_id=conversation_id,
        base_plan_version=plan.version,
        enabled_transaction_types=sorted(set(enabled_types)),
        proposed_at=at,
    )
    world.approvals.append(proposal)
    world.advance_to(at)
    return proposal


def find_approval(world: World, approval_id: str) -> Approval:
    proposal = next((item for item in world.approvals if item.id == approval_id), None)
    if proposal is None:
        raise DomainError("Approval not found", 404)
    return proposal


def approve_change(
    world: World, approval_id: str, at: datetime, *, session_id: str | None = None
) -> Approval:
    proposal = find_approval(world, approval_id)
    if proposal.session_managed and proposal.conversation_id != session_id:
        raise DomainError("Managed proposal requires session approval")
    if at < world.clock:
        raise DomainError("Cannot approve a change in the past")
    if proposal.status == "pending":
        proposal.status = "approved"
        proposal.approved_at = at
    world.advance_to(at)
    return proposal


def apply_change(
    world: World, approval_id: str, at: datetime, *, session_id: str | None = None
) -> SavingsPlan:
    proposal = find_approval(world, approval_id)
    if proposal.session_managed and proposal.conversation_id != session_id:
        raise DomainError("Managed proposal requires session application")
    if proposal.status == "applied":
        return next(p for p in world.plans if p.version == proposal.applied_version)
    if proposal.status != "approved":
        raise DomainError("Customer approval is required")
    if at < world.clock:
        raise DomainError("Cannot apply a change in the past")
    current = get_plan(world, at)
    if current.version != proposal.base_plan_version:
        raise DomainError("Plan changed since proposal; request fresh approval")
    plan = current.model_copy(
        update={
            "version": current.version + 1,
            "effective_from": at,
            "enabled_transaction_types": list(proposal.enabled_transaction_types),
        }
    )
    world.plans.append(plan)
    proposal.status = "applied"
    proposal.applied_version = plan.version
    world.advance_to(at)
    return plan


def initial_world() -> World:
    """Seed facts only. All results are computed by the same generic rules."""
    world = World(
        customer=Customer(id="customer-001", display_name="Ada (fictional)"),
        clock=datetime(2026, 10, 21, 10, 0, tzinfo=UTC),
        plans=[
            SavingsPlan(
                customer_id="customer-001",
                version=1,
                effective_from=datetime(2026, 10, 21, 9, 0, tzinfo=UTC),
                enabled_transaction_types=["card_payment"],
                fixed_contribution_minor=50000,
            )
        ],
    )
    create_transaction(
        world,
        NewTransaction(
            transaction_id="txn-001",
            type="transfer",
            amount_minor=2000000,
            occurred_at=datetime(2026, 10, 21, 10, 0, tzinfo=UTC),
        ),
    )
    return world
