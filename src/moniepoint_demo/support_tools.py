"""Typed model-visible tools with identity injected by the application."""

from typing import Any

from pydantic import AwareDatetime, Field, model_validator

from moniepoint_demo.chat import PolicyChat
from moniepoint_demo.models import Record, TransactionStatus, TransactionType
from moniepoint_demo.policy_search import SearchRequest
from moniepoint_demo.simulator import DomainError, get_plan
from moniepoint_demo.support_service import SupportService
from moniepoint_demo.tool_descriptions import DESCRIPTIONS


class FindTransactions(Record):
    since: AwareDatetime | None = None
    until: AwareDatetime | None = None
    type: TransactionType | None = None
    status: TransactionStatus | None = None

    @model_validator(mode="after")
    def ordered_dates(self) -> "FindTransactions":
        if self.since and self.until and self.since > self.until:
            raise ValueError("since must not be later than until")
        return self


class TransactionRef(Record):
    transaction_id: str = Field(min_length=1, max_length=64)


class PlanQuery(Record):
    at: AwareDatetime | None = None


class PolicyQuestion(Record):
    question: str = Field(min_length=1, max_length=2000)


class ProposedSettings(Record):
    enabled_transaction_types: list[TransactionType] = Field(max_length=4)


class ProposalRef(Record):
    proposal_id: str = Field(min_length=1, max_length=64)


ARGUMENT_MODELS: dict[str, type[Record]] = {
    "find_transactions": FindTransactions,
    "get_transaction": TransactionRef,
    "get_savings_plan": PlanQuery,
    "get_sayt_processing": TransactionRef,
    "get_savings_contributions": TransactionRef,
    "ask_policy_question": PolicyQuestion,
    "propose_savings_plan_change": ProposedSettings,
    "request_plan_change_approval": ProposalRef,
}


class ToolContract(Record):
    name: str
    description: str
    input_schema: dict[str, Any]


def tool_catalog() -> list[ToolContract]:
    return [
        ToolContract(
            name=name,
            description=DESCRIPTIONS[name],
            input_schema=model.model_json_schema(),
        )
        for name, model in ARGUMENT_MODELS.items()
    ]


class SupportTools:
    def __init__(
        self,
        service: SupportService,
        session_id: str,
        policy_chat: PolicyChat | None = None,
    ) -> None:
        self.service = service
        self.session_id = session_id
        self.policy_chat = policy_chat

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        operation_id: str,
    ) -> dict[str, Any]:
        session = self.service.session(self.session_id)
        model = ARGUMENT_MODELS.get(name)
        if model is None:
            raise DomainError("Unknown support tool", 422)
        args = model.model_validate(arguments)
        world = self.service.store.read()
        if isinstance(args, FindTransactions):
            return {
                "transactions": [
                    tx.model_dump(mode="json")
                    for tx in world.transactions
                    if (args.since is None or tx.occurred_at >= args.since)
                    and (args.until is None or tx.occurred_at <= args.until)
                    and (args.type is None or tx.type == args.type)
                    and (args.status is None or tx.status == args.status)
                ]
            }
        if isinstance(args, TransactionRef):
            tx = next(
                (
                    tx
                    for tx in world.transactions
                    if tx.transaction_id == args.transaction_id
                ),
                None,
            )
            if tx is None:
                raise DomainError("Transaction not found", 404)
            if name == "get_transaction":
                return tx.model_dump(mode="json")
            if name == "get_sayt_processing":
                record = next(
                    (
                        r
                        for r in world.processing
                        if r.transaction_id == args.transaction_id
                    ),
                    None,
                )
                return {
                    "processing": record.model_dump(mode="json") if record else None
                }
            return {
                "contributions": [
                    c.model_dump(mode="json")
                    for c in world.contributions
                    if c.transaction_id == args.transaction_id
                ]
            }
        if isinstance(args, PlanQuery):
            return get_plan(world, args.at).model_dump(mode="json")
        if isinstance(args, PolicyQuestion):
            if self.policy_chat is None:
                raise DomainError("Policy service is unavailable", 503)
            return self.policy_chat.ask(
                SearchRequest(
                    query=args.question,
                    use_cache=session.use_cache,
                )
            ).model_dump(mode="json")
        if isinstance(args, ProposedSettings):
            return self.service.propose(
                self.session_id,
                args.enabled_transaction_types,
                operation_id,
            ).model_dump(mode="json")
        if isinstance(args, ProposalRef):
            return self.service.request_approval(
                self.session_id,
                args.proposal_id,
            ).model_dump(mode="json")
        raise DomainError("Unsupported tool arguments", 422)
