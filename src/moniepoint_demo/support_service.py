"""Session ownership and atomic proposal decisions, independent of agent output."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import uuid4

from moniepoint_demo.models import (
    Approval,
    SavingsPlan,
    SupportSession,
    TransactionType,
    World,
)
from moniepoint_demo.simulator import (
    DomainError,
    apply_change,
    get_plan,
    propose_change,
)
from moniepoint_demo.store import RedisWorldStore


def owned_session(world: World, session_id: str) -> SupportSession:
    for session in world.support_sessions:
        if session.id == session_id and session.customer_id == world.customer.id:
            return session
    raise DomainError("Session not found; it may have been reset", 404)


def owned_proposal(world: World, session_id: str, proposal_id: str) -> Approval:
    owned_session(world, session_id)
    for proposal in world.approvals:
        if (
            proposal.id == proposal_id
            and proposal.conversation_id == session_id
            and proposal.customer_id == world.customer.id
            and proposal.session_managed
        ):
            return proposal
    raise DomainError("Proposal not found", 404)


def validate_decision(
    world: World,
    session_id: str,
    proposal_id: str,
    decision: Literal["approve", "decline"],
    request_id: str,
) -> Approval:
    proposal = owned_proposal(world, session_id, proposal_id)
    for previous in world.approvals:
        if (
            previous.conversation_id == session_id
            and previous.decision_request_id == request_id
        ):
            if previous.id != proposal_id or previous.decision != decision:
                raise DomainError("Request ID reused with different decision")
    if proposal.status in ("stale", "superseded"):
        raise DomainError("Proposal is no longer active; request a fresh proposal")
    if proposal.decision is not None:
        if proposal.decision == decision:
            return proposal
        raise DomainError("A different decision is already recorded")
    if proposal.status != "pending":
        raise DomainError("Proposal is no longer active")
    if not proposal.presented:
        raise DomainError("Proposal must be presented before a decision")
    if get_plan(world, None).version != proposal.base_plan_version:
        raise DomainError("Plan changed; create a fresh proposal")
    return proposal


class SupportService:
    def __init__(self, store: RedisWorldStore) -> None:
        self.store = store

    def create_session(
        self, use_cache: bool = True, use_memory: bool = False, use_history: bool = True
    ) -> SupportSession:
        session = SupportSession(
            id=uuid4().hex,
            customer_id=self.store.customer_id,
            created_at=datetime.now(UTC),
            use_cache=use_cache,
            use_memory=use_memory,
            use_history=use_history,
        )

        def create(world: World) -> SupportSession:
            world.support_sessions.append(session)
            return session

        return self.store.mutate(create)

    def session(self, session_id: str) -> SupportSession:
        return owned_session(self.store.read(), session_id)

    def propose(
        self, session_id: str, enabled_types: list[TransactionType], operation_id: str
    ) -> Approval:
        def change(world: World) -> Approval:
            owned_session(world, session_id)
            desired = sorted(set(enabled_types))
            for proposal in world.approvals:
                if (
                    proposal.session_managed
                    and proposal.conversation_id == session_id
                    and proposal.operation_id == operation_id
                ):
                    if proposal.enabled_transaction_types != desired:
                        raise DomainError(
                            "Operation ID reused with different arguments"
                        )
                    return proposal
            plan = get_plan(world, None)
            if sorted(plan.enabled_transaction_types) == desired:
                raise DomainError("Proposal would make no change", 422)
            active = [
                p
                for p in world.approvals
                if p.session_managed
                and p.conversation_id == session_id
                and p.status in ("pending", "approved")
            ]
            if any(p.status == "approved" for p in active):
                raise DomainError("Approved proposal must finish before replacement")
            for previous in active:
                previous.status = "superseded"
            proposal = propose_change(
                world, enabled_types, session_id, world.clock + timedelta(seconds=1)
            )
            proposal.session_managed = True
            proposal.operation_id = operation_id
            proposal.before_enabled_transaction_types = list(
                plan.enabled_transaction_types
            )
            return proposal

        return self.store.mutate(change)

    def request_approval(self, session_id: str, proposal_id: str) -> Approval:
        def present(world: World) -> Approval:
            proposal = owned_proposal(world, session_id, proposal_id)
            if proposal.status != "pending":
                raise DomainError("Proposal is no longer active")
            if get_plan(world, None).version != proposal.base_plan_version:
                raise DomainError("Plan changed; create a fresh proposal")
            proposal.presented = True
            return proposal

        return self.store.mutate(present)

    def validate_decision(
        self,
        session_id: str,
        proposal_id: str,
        decision: Literal["approve", "decline"],
        request_id: str,
    ) -> Approval:
        return validate_decision(
            self.store.read(), session_id, proposal_id, decision, request_id
        )

    def decide(
        self,
        session_id: str,
        proposal_id: str,
        decision: Literal["approve", "decline"],
        request_id: str,
    ) -> Approval:
        """Application-only entry point; never register as an agent tool."""

        def record(world: World) -> Approval:
            proposal = validate_decision(
                world, session_id, proposal_id, decision, request_id
            )
            if proposal.decision is not None:
                return proposal
            proposal.decision = decision
            proposal.decision_request_id = request_id
            proposal.status = "approved" if decision == "approve" else "declined"
            if decision == "approve":
                proposal.approved_at = world.clock + timedelta(seconds=1)
                world.advance_to(proposal.approved_at)
            return proposal

        return self.store.mutate(record)

    def apply(self, session_id: str, proposal_id: str) -> SavingsPlan:
        """Execute the recorded decision; retries return the original applied plan."""

        def apply(world: World) -> SavingsPlan | None:
            proposal = owned_proposal(world, session_id, proposal_id)
            if proposal.decision != "approve":
                raise DomainError("Customer approval is required")
            if (
                proposal.status != "applied"
                and get_plan(world, None).version != proposal.base_plan_version
            ):
                proposal.status = "stale"
                return None
            return apply_change(
                world,
                proposal_id,
                world.clock + timedelta(seconds=1),
                session_id=session_id,
            )

        result = self.store.mutate(apply)
        if result is None:
            raise DomainError("Plan changed; create a fresh proposal")
        return result
