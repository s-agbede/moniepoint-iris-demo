"""Fictional customer facts, processed by the simulator's ordinary rules."""

from datetime import UTC, datetime, timedelta

from moniepoint_demo.models import (
    Customer,
    NewTransaction,
    SavingsPlan,
    TransactionStatus,
    TransactionType,
    World,
)
from moniepoint_demo.simulator import DomainError, create_transaction

CUSTOMER_IDS = (
    "customer-001",
    "customer-002",
    "customer-003",
    "customer-004",
    "customer-005",
)


def demo_customers() -> list[Customer]:
    return [
        Customer(id="customer-001", display_name="Ada (fictional)"),
        Customer(id="customer-002", display_name="Chidi (fictional)"),
        Customer(id="customer-003", display_name="Bisi (fictional)"),
        Customer(id="customer-004", display_name="Emeka (fictional)"),
        Customer(id="customer-005", display_name="Zainab (fictional)"),
    ]


def demo_world(customer_id: str = "customer-001") -> World:
    customer = next((c for c in demo_customers() if c.id == customer_id), None)
    if customer is None:
        raise DomainError("Demo customer not found", 404)
    enabled: list[TransactionType]
    if customer_id in ("customer-001", "customer-004"):
        enabled, contribution = ["card_payment"], 50000
    elif customer_id in ("customer-002", "customer-005"):
        enabled, contribution = ["card_payment", "transfer"], 25000
    else:
        enabled, contribution = ["airtime", "bill_payment"], 10000

    start = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
    world = World(
        customer=customer,
        clock=start + timedelta(hours=1),
        plans=[
            SavingsPlan(
                id=f"plan-{customer_id.removeprefix('customer-')}",
                customer_id=customer_id,
                version=1,
                effective_from=start,
                enabled_transaction_types=enabled,
                fixed_contribution_minor=contribution,
            )
        ],
    )
    # Explicit sample facts. These rows do not select an agent's answer or next tool.
    rows: list[tuple[str, TransactionType, int, int, TransactionStatus, int]]
    if customer_id == "customer-001":
        rows = [
            ("txn-001", "transfer", 2000000, 60, "completed", 5),
            ("ada-card-001", "card_payment", 850000, 10, "completed", 5),
            ("ada-transfer-002", "transfer", 1200000, 20, "completed", 5),
            ("ada-card-late", "card_payment", 500000, 30, "completed", 180),
            ("ada-airtime-failed", "airtime", 100000, 40, "failed", 5),
            ("ada-transfer-pending", "transfer", 3000000, 50, "pending", 5),
        ]
    elif customer_id == "customer-002":
        rows = [
            ("chidi-transfer-001", "transfer", 1000000, 10, "completed", 5),
            ("chidi-card-001", "card_payment", 750000, 20, "completed", 5),
            ("chidi-transfer-late", "transfer", 2500000, 30, "completed", 180),
            ("chidi-bill-001", "bill_payment", 500000, 40, "completed", 5),
            ("chidi-transfer-failed", "transfer", 1500000, 50, "failed", 5),
            ("chidi-transfer-pending", "transfer", 4000000, 55, "pending", 5),
        ]
    elif customer_id == "customer-004":
        rows = [
            ("emeka-transfer-001", "transfer", 1200000, 10, "completed", 5),
            ("emeka-card-001", "card_payment", 850000, 20, "completed", 5),
            ("emeka-transfer-002", "transfer", 2000000, 30, "completed", 5),
        ]
    elif customer_id == "customer-005":
        rows = [
            ("zainab-transfer-001", "transfer", 1000000, 10, "completed", 5),
            ("zainab-card-001", "card_payment", 750000, 20, "completed", 5),
            ("zainab-transfer-late", "transfer", 2500000, 30, "completed", 180),
        ]
    else:
        rows = [
            ("bisi-airtime-001", "airtime", 200000, 10, "completed", 5),
            ("bisi-bill-001", "bill_payment", 1000000, 20, "completed", 5),
            ("bisi-transfer-001", "transfer", 3500000, 30, "completed", 5),
            ("bisi-bill-late", "bill_payment", 650000, 40, "completed", 180),
            ("bisi-airtime-failed", "airtime", 100000, 50, "failed", 5),
            ("bisi-bill-pending", "bill_payment", 2000000, 55, "pending", 5),
        ]
    for identifier, kind, amount, minute, status, delay in rows:
        create_transaction(
            world,
            NewTransaction(
                transaction_id=identifier,
                type=kind,
                amount_minor=amount,
                occurred_at=start + timedelta(minutes=minute),
                status=status,
                delivery_delay_seconds=delay,
            ),
        )
    return world
