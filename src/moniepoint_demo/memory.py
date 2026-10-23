"""Customer-scoped Iris observations; regex redaction is a demo guard, not full DLP."""

import logging
import os
import re
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Literal

from pydantic import Field
from redis_agent_memory import AgentMemory, models

from moniepoint_demo.models import Record

logger = logging.getLogger(__name__)


def redact(text: str) -> str:
    """Remove common demo identifiers before any Iris request or event write."""
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[REDACTED]", text)
    text = re.sub(
        r"(?i)\b(pin|password|passcode|otp|api[_ -]?key|access[_ -]?token)"
        r'([\s"\x27:=]+)([^\s,;."\x27}]+)',
        r"\1: [REDACTED]",
        text,
    )
    # International phone, account, card and BVN-style digit sequences.
    text = re.sub(r"(?<![\w])\+?\d(?:[ ()-]?\d){9,18}(?![\w])", "[REDACTED]", text)
    return text


def redact_values(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_values(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_values(item) for key, item in value.items()}
    return value


class MemoryHit(Record):
    id: str
    text: str
    memory_type: str | None = None
    session_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class MemorySearch(Record):
    status: Literal["disabled", "ready", "unavailable"] = "disabled"
    memories: list[MemoryHit] = Field(default_factory=list)
    duration_ms: float = 0
    error: str | None = None


class MemoryRecording(Record):
    status: Literal["disabled", "recorded", "unavailable", "partial"] = "disabled"
    events: list[dict[str, str]] = Field(default_factory=list)
    duration_ms: float = 0
    error: str | None = None


class IrisMemory:
    def __init__(self, client: Any) -> None:
        self.client = client

    @staticmethod
    def configured() -> bool:
        return all(
            os.environ.get(k)
            for k in (
                "AGENT_MEMORY_URL",
                "AGENT_MEMORY_STORE_ID",
                "AGENT_MEMORY_API_KEY",
            )
        )

    @classmethod
    def from_environment(cls) -> "IrisMemory":
        return cls(
            AgentMemory(
                os.environ["AGENT_MEMORY_URL"],
                store_id=os.environ["AGENT_MEMORY_STORE_ID"],
                api_key=os.environ["AGENT_MEMORY_API_KEY"],
            )
        )

    @staticmethod
    def owner(customer_id: str) -> str:
        return f"moniepoint-demo-{customer_id}"

    def search(self, customer_id: str, query: str) -> MemorySearch:
        started = perf_counter()
        try:
            response = self.client.search_long_term_memory(
                request={
                    "text": redact(query),
                    "filter_": {
                        "owner_id": {"eq": self.owner(customer_id)},
                        "memory_type": {"eq": "support_observation"},
                    },
                    "limit": 5,
                },
                timeout_ms=10000,
            )
            hits = [
                MemoryHit(
                    id=m.id,
                    text=redact(m.text),
                    memory_type=m.memory_type,
                    session_id=m.session_id,
                    attributes=redact_values(m.attributes or {}),
                )
                for m in response.items
                if m.owner_id == self.owner(customer_id)
            ]
            result = MemorySearch(status="ready", memories=hits)
        except Exception as error:
            logger.warning("Iris search unavailable: %s", type(error).__name__)
            result = MemorySearch(
                status="unavailable",
                error="Memory lookup unavailable; using live tools.",
            )
        result.duration_ms = (perf_counter() - started) * 1000
        return result

    def record(
        self,
        customer_id: str,
        session_id: str,
        turn_id: str,
        events: list[tuple[str, str]],
    ) -> MemoryRecording:
        started = perf_counter()
        result = MemoryRecording(status="recorded")
        try:
            for role, content in events:
                safe = redact(content)
                self.client.add_session_event(
                    session_id=session_id,
                    actor_id=self.owner(customer_id),
                    role=models.MessageRole(role),
                    content=[models.Text(text=safe)],
                    created_at=datetime.now(UTC),
                    metadata={"turn_id": turn_id, "source_role": role},
                    timeout_ms=10000,
                )
                result.events.append({"role": role, "text": safe})
        except Exception as error:
            logger.warning("Iris event write unavailable: %s", type(error).__name__)
            result.status = "partial" if result.events else "unavailable"
            result.error = (
                "Some events could not be confirmed. Extraction may be "
                "incomplete; writes are not automatically retried."
            )
        result.duration_ms = (perf_counter() - started) * 1000
        return result

    def close(self) -> None:
        self.client.__exit__(None, None, None)
