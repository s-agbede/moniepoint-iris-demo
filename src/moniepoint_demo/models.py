"""Explicit records for a fictional, fixed-amount SAYT simulator."""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

TransactionType = Literal["transfer", "card_payment", "airtime", "bill_payment"]
TransactionStatus = Literal["completed", "pending", "failed"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Customer(Record):
    id: str
    display_name: str


class NewTransaction(Record):
    transaction_id: str = Field(min_length=1, max_length=64)
    type: TransactionType
    amount_minor: int = Field(gt=0)
    currency: Literal["NGN"] = "NGN"
    status: TransactionStatus = "completed"
    occurred_at: AwareDatetime
    delivery_delay_seconds: int = Field(default=5, ge=0, le=86400)


class Transaction(NewTransaction):
    customer_id: str


class SavingsPlan(Record):
    id: str = "plan-001"
    customer_id: str
    version: int = Field(ge=1)
    effective_from: AwareDatetime
    enabled_transaction_types: list[TransactionType] = Field(
        description="Transaction types that trigger a contribution under this plan."
    )
    fixed_contribution_minor: int = Field(
        gt=0,
        description="Fixed savings per eligible transaction in kobo; 50000 = NGN 500.",
    )
    currency: Literal["NGN"] = "NGN"


class ProcessingRecord(Record):
    event_id: str
    transaction_id: str
    plan_version_used: int
    produced_at: AwareDatetime
    received_at: AwareDatetime
    status: Literal["excluded", "dropped", "processed"]
    reason: Literal["transaction_type_excluded", "stale_event", "contribution_posted"]


class Contribution(Record):
    id: str
    customer_id: str
    plan_id: str
    transaction_id: str
    event_id: str
    amount_minor: int
    currency: Literal["NGN"] = "NGN"
    posted_at: AwareDatetime


class Approval(Record):
    id: str
    customer_id: str
    conversation_id: str
    base_plan_version: int
    enabled_transaction_types: list[TransactionType]
    status: Literal[
        "pending", "approved", "applied", "declined", "superseded", "stale"
    ] = "pending"
    session_managed: bool = False
    operation_id: str | None = None
    before_enabled_transaction_types: list[TransactionType] = Field(
        default_factory=list
    )
    presented: bool = False
    decision: Literal["approve", "decline"] | None = None
    decision_request_id: str | None = None
    proposed_at: AwareDatetime
    approved_at: AwareDatetime | None = None
    applied_version: int | None = None


class SupportSession(Record):
    id: str
    customer_id: str
    created_at: AwareDatetime
    use_cache: bool = True
    use_memory: bool = False
    use_history: bool = True


class World(Record):
    customer: Customer
    clock: AwareDatetime
    freshness_window_seconds: int = 120
    transactions: list[Transaction] = Field(default_factory=list)
    plans: list[SavingsPlan] = Field(default_factory=list)
    processing: list[ProcessingRecord] = Field(default_factory=list)
    contributions: list[Contribution] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    support_sessions: list[SupportSession] = Field(default_factory=list)

    def advance_to(self, instant: datetime) -> None:
        self.clock = max(self.clock, instant)


class PlanChangeRequest(Record):
    enabled_transaction_types: list[TransactionType] = Field(max_length=4)
    conversation_id: str = Field(min_length=1, max_length=64)
