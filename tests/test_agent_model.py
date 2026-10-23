"""The provider adapter preserves tool IDs and rejects incomplete responses."""

from types import SimpleNamespace
from typing import Any

import pytest

from moniepoint_demo import support_agent
from moniepoint_demo.simulator import DomainError


def fake_provider(monkeypatch: pytest.MonkeyPatch, response: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            self.responses = self

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def create(self, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return response

    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.setattr(support_agent, "OpenAI", Client)
    return captured


def test_responses_preserves_tool_history_and_new_call_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = fake_provider(
        monkeypatch,
        SimpleNamespace(
            status="completed",
            output_text="",
            output=[
                SimpleNamespace(
                    type="function_call",
                    call_id="next-call",
                    name="get_savings_plan",
                    arguments="{}",
                )
            ],
        ),
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "Support instructions"},
        {"role": "user", "content": "Investigate"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "prior-call",
                    "type": "function",
                    "function": {
                        "name": "get_transaction",
                        "arguments": '{"transaction_id":"t1"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "prior-call",
            "content": '{"status":"completed"}',
        },
    ]
    reply = support_agent.OpenAIAgentModel().respond(messages)
    assert captured["model"] == "gpt-6.1-sol"
    assert captured["reasoning"] == {"effort": "low"}
    assert captured["store"] is False
    assert captured["input"][2]["call_id"] == "prior-call"
    assert captured["input"][3] == {
        "type": "function_call_output",
        "call_id": "prior-call",
        "output": '{"status":"completed"}',
    }
    assert reply["tool_calls"][0]["id"] == "next-call"
    assert reply["tool_calls"][0]["function"]["name"] == "get_savings_plan"
    assert messages[2]["content"] is None


@pytest.mark.parametrize(
    "status,text", [("incomplete", "Partial answer"), ("completed", "")]
)
def test_rejects_partial_or_empty_provider_answers(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    text: str,
) -> None:
    fake_provider(
        monkeypatch, SimpleNamespace(status=status, output_text=text, output=[])
    )
    with pytest.raises(DomainError):
        support_agent.OpenAIAgentModel().respond([{"role": "user", "content": "Help"}])


def test_returns_completed_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_provider(
        monkeypatch,
        SimpleNamespace(
            status="completed", output_text="No settings change is needed.", output=[]
        ),
    )
    assert support_agent.OpenAIAgentModel().respond(
        [{"role": "user", "content": "Help"}]
    ) == {"role": "assistant", "content": "No settings change is needed."}
