from types import SimpleNamespace
from typing import Any

from moniepoint_demo.memory import IrisMemory, redact


class SDK:
    def __init__(self) -> None:
        self.searches: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    def search_long_term_memory(self, **kwargs: Any) -> Any:
        self.searches.append(kwargs)
        return SimpleNamespace(items=[])

    def add_session_event(self, **kwargs: Any) -> Any:
        self.events.append(kwargs)
        return SimpleNamespace(event_id="event-1")


def test_redaction_preserves_demo_references_and_money() -> None:
    text = redact(
        "Email ada@example.com, phone +234 801 234 5678, PIN: 1234. "
        "Account number 0123456789; card 4111 1111 1111 1111. "
        "Transfer ada-transfer-002 was NGN 12,000."
    )
    for secret in ["ada@example.com", "801 234 5678", "1234", "0123456789", "4111"]:
        assert secret not in text
    assert "ada-transfer-002" in text
    assert "NGN 12,000" in text


def test_retrieval_scopes_owner_and_redacts_query() -> None:
    sdk = SDK()
    memory = IrisMemory(sdk)
    result = memory.search("customer-001", "Contact me at ada@example.com")
    request = sdk.searches[0]["request"]
    assert request["filter_"]["owner_id"] == {"eq": "moniepoint-demo-customer-001"}
    assert request["filter_"]["memory_type"] == {"eq": "support_observation"}
    assert "ada@example.com" not in request["text"]
    assert result.status == "ready"


def test_record_redacts_every_event_and_binds_owner() -> None:
    sdk = SDK()
    memory = IrisMemory(sdk)
    result = memory.record(
        "customer-002",
        "session-1",
        "turn-1",
        [("USER", "PIN: 4321"), ("SYSTEM", "Email b@x.com")],
    )
    assert result.status == "recorded"
    assert len(sdk.events) == 2
    assert all(e["actor_id"] == "moniepoint-demo-customer-002" for e in sdk.events)
    assert "4321" not in str(sdk.events)
    assert "b@x.com" not in str(sdk.events)
    assert sdk.events[0]["session_id"] == "session-1"


def test_memory_failure_is_explicit_not_fatal() -> None:
    class Broken(SDK):
        def search_long_term_memory(self, **kwargs: Any) -> Any:
            raise RuntimeError("secret response body")

    result = IrisMemory(Broken()).search("customer-001", "transfer")
    assert result.status == "unavailable"
    assert "secret" not in str(result)


def test_returned_records_are_checked_and_attributes_redacted() -> None:
    class Returned(SDK):
        def search_long_term_memory(self, **kwargs: Any) -> Any:
            def hit(owner: str) -> Any:
                return SimpleNamespace(
                    id="m1",
                    owner_id=owner,
                    text="Email a@b.com",
                    memory_type="support_observation",
                    session_id="s1",
                    attributes={"summary": "PIN: 1234", "nested": ["a@b.com"]},
                )

            return SimpleNamespace(
                items=[hit("other-customer"), hit("moniepoint-demo-customer-001")]
            )

    result = IrisMemory(Returned()).search("customer-001", "transfer")
    assert len(result.memories) == 1
    assert "a@b.com" not in result.model_dump_json()
    assert "1234" not in result.model_dump_json()
