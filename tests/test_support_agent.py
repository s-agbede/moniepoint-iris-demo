from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from redis import Redis

from moniepoint_demo.simulator import DomainError
from moniepoint_demo.store import RedisWorldStore
from moniepoint_demo.support_service import SupportService


class Model:
    model = "controlled-test-model"

    def __init__(self, replies: list[dict[str, Any] | Exception]) -> None:
        self.replies = replies
        self.seen: list[list[dict[str, Any]]] = []

    def respond(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        self.seen.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def call(name: str, arguments: str = "{}") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": uuid4().hex,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments,
                },
            }
        ],
    }


def answer(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


@pytest.fixture
def store() -> Iterator[RedisWorldStore]:
    client = Redis.from_url("redis://localhost:6387/0", decode_responses=True)
    key = f"agent-test:{uuid4().hex}"
    world = RedisWorldStore(client, key)
    world.initialize()
    yield world
    for index in client.execute_command("FT._LIST"):  # type: ignore[no-untyped-call]
        if str(index).startswith(key):
            client.execute_command("FT.DROPINDEX", index, "DD")  # type: ignore[no-untyped-call]
    keys = list(client.scan_iter(match=f"{key}*"))
    if keys:
        client.delete(*keys)
    client.close()


def test_checkpoint_restore_trace_and_request_replay(store: RedisWorldStore) -> None:
    from moniepoint_demo.support_agent import MessageRequest, SupportAgent

    session = SupportService(store).create_session()
    model = Model([call("get_savings_plan"), answer("Card payments are enabled.")])
    runner = SupportAgent(store, model)
    request = MessageRequest(message="What is enabled?", request_id="first")
    result = runner.ask(session.id, request)
    assert result.status == "completed"
    assert result.trace[0].name == "get_savings_plan"
    assert result.trace[0].result["customer_id"] == "customer-001"
    assert result.timings.total_ms > 0
    assert runner.ask(session.id, request).turn_id == result.turn_id
    assert len(model.seen) == 2
    assert [t.turn_id for t in runner.inspect(session.id).turns] == [result.turn_id]
    fresh_model = Model([answer("We checked your card-payment settings.")])
    restarted = SupportAgent(store, fresh_model)
    restarted.ask(
        session.id, MessageRequest(message="What did we check?", request_id="second")
    )
    assert any(m.get("content") == "What is enabled?" for m in fresh_model.seen[0])
    with pytest.raises(DomainError, match="different"):
        restarted.ask(
            session.id, MessageRequest(message="different", request_id="first")
        )


def test_failed_model_call_resumes_without_duplicate_message(
    store: RedisWorldStore,
) -> None:
    from moniepoint_demo.support_agent import MessageRequest, SupportAgent

    session = SupportService(store).create_session()
    runner = SupportAgent(
        store, Model([call("get_savings_plan"), DomainError("Model unavailable", 503)])
    )
    request = MessageRequest(message="Inspect my plan", request_id="retry")
    failed = runner.ask(session.id, request)
    assert failed.status == "failed"
    with pytest.raises(DomainError, match="Retry"):
        runner.ask(session.id, MessageRequest(message="new turn", request_id="other"))
    model = Model([answer("Your plan was checked.")])
    recovered = SupportAgent(store, model).ask(session.id, request)
    assert recovered.status == "completed"
    assert len(recovered.trace) == 1
    assert sum(m.get("content") == "Inspect my plan" for m in model.seen[0]) == 1


def test_invalid_tools_limits_and_reset(store: RedisWorldStore) -> None:
    from moniepoint_demo.support_agent import MessageRequest, SupportAgent

    session = SupportService(store).create_session()
    model = Model(
        [call("get_savings_plan", '{"customer_id":"customer-002"}') for _ in range(8)]
    )
    runner = SupportAgent(store, model)
    result = runner.ask(session.id, MessageRequest(message="Check", request_id="limit"))
    assert result.status == "limit_reached"
    assert all(t.error for t in result.trace)
    store.reset()
    with pytest.raises(DomainError, match="Session not found"):
        runner.inspect(session.id)


def test_http_session_isolation_and_trace(store: RedisWorldStore) -> None:
    from fastapi.testclient import TestClient

    from moniepoint_demo.api import create_app

    model = Model([call("get_savings_plan"), answer("Your card savings are enabled.")])
    with TestClient(create_app(store, agent_model=model)) as client:
        session_response = client.post("/support/sessions", json={"use_cache": False})
        assert session_response.status_code == 201
        session_id = session_response.json()["id"]
        assert [s["id"] for s in client.get("/support/sessions").json()] == [session_id]
        assert (
            client.get(
                "/support/sessions", headers={"X-Demo-Customer-Id": "customer-002"}
            ).json()
            == []
        )
        route = f"/support/sessions/{session_id}"
        denied = client.post(
            route + "/messages",
            json={"message": "Check my plan", "request_id": "1"},
            headers={"X-Demo-Customer-Id": "customer-002"},
        )
        assert denied.status_code == 404
        response = client.post(
            route + "/messages", json={"message": "Check my plan", "request_id": "1"}
        )
        assert response.status_code == 200
        turn = response.json()
        assert turn["status"] == "completed"
        assert client.get(route).json()["session"]["use_cache"] is False
        assert client.get(route + "/turns/" + turn["turn_id"]).json()["trace"]
        assert (
            client.post(
                route + "/messages", json={"message": " ", "request_id": "2"}
            ).status_code
            == 422
        )


def test_pending_card_allows_followup_and_no_model_approval_tool(
    store: RedisWorldStore,
) -> None:
    from moniepoint_demo.support_agent import MessageRequest, SupportAgent

    service = SupportService(store)
    session = service.create_session()
    proposal = service.propose(session.id, ["transfer", "card_payment"], "p1")
    model = Model(
        [
            call(
                "request_plan_change_approval", '{"proposal_id":"' + proposal.id + '"}'
            ),
            answer("Your change is pending."),
            call("approve_change", "{}"),
            answer("Please use the approval control when available."),
        ]
    )
    runner = SupportAgent(store, model)
    first = runner.ask(
        session.id, MessageRequest(message="Show the change", request_id="1")
    )
    assert first.status == "awaiting_approval"
    assert first.approval and first.approval.id == proposal.id
    followup = runner.ask(session.id, MessageRequest(message="yes", request_id="2"))
    assert followup.trace[0].error
    assert len(store.read().plans) == 1
    assert store.read().approvals[0].status == "pending"


def test_busy_session_rejects_concurrent_turn(store: RedisWorldStore) -> None:
    from moniepoint_demo.support_agent import MessageRequest, SupportAgent

    session = SupportService(store).create_session()
    runner = SupportAgent(store, Model([]))
    lock = store.client.lock(f"{runner.root}:{session.id}:lock", timeout=30)
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(DomainError, match="busy"):
            runner.ask(session.id, MessageRequest(message="check", request_id="1"))
    finally:
        lock.release()


def test_tool_call_budget_completes_protocol_without_more_execution(
    store: RedisWorldStore,
) -> None:
    from moniepoint_demo.support_agent import MessageRequest, SupportAgent

    session = SupportService(store).create_session()
    response = call("get_savings_plan")
    response["tool_calls"] = [
        call("get_savings_plan")["tool_calls"][0] for _ in range(21)
    ]
    runner = SupportAgent(store, Model([response]))
    result = runner.ask(session.id, MessageRequest(message="check", request_id="1"))
    assert result.status == "limit_reached"
    assert len(result.trace) == 21
    assert sum(not item.error for item in result.trace) == 20
    history = runner.inspect(session.id).messages
    assert len([message for message in history if message["role"] == "tool"]) == 21


def test_approval_applies_before_explanation_and_recovers(
    store: RedisWorldStore,
) -> None:
    from moniepoint_demo.support_agent import DecisionRequest, SupportAgent

    service = SupportService(store)
    session = service.create_session()
    proposal = service.propose(session.id, ["card_payment", "transfer"], "p1")
    service.request_approval(session.id, proposal.id)
    runner = SupportAgent(store, Model([DomainError("Model unavailable", 503)]))
    request = DecisionRequest(decision="approve", request_id="approve-1")
    failed = runner.decide(session.id, proposal.id, request)
    assert failed.status == "failed"
    assert failed.action and failed.action["status"] == "applied"
    assert len(store.read().plans) == 2
    model = Model([answer("Your plan change was applied.")])
    recovered = SupportAgent(store, model).decide(session.id, proposal.id, request)
    assert recovered.status == "completed"
    assert recovered.action and recovered.action["status"] == "applied"
    assert len(store.read().plans) == 2
    assert '"status": "applied"' in model.seen[0][0]["content"]
    assert runner.decide(session.id, proposal.id, request).turn_id == recovered.turn_id
    with pytest.raises(DomainError):
        runner.decide(
            session.id,
            proposal.id,
            DecisionRequest(decision="decline", request_id="approve-1"),
        )


def test_decline_and_stale_decisions_do_not_change_plan(store: RedisWorldStore) -> None:
    from moniepoint_demo.support_agent import DecisionRequest, SupportAgent

    service = SupportService(store)
    session = service.create_session()
    old = service.propose(session.id, ["transfer"], "p1")
    service.request_approval(session.id, old.id)
    current = service.propose(session.id, ["airtime"], "p2")
    service.request_approval(session.id, current.id)
    runner = SupportAgent(store, Model([answer("The change was declined.")]))
    with pytest.raises(DomainError):
        runner.decide(
            session.id, old.id, DecisionRequest(decision="approve", request_id="old")
        )
    declined = runner.decide(
        session.id, current.id, DecisionRequest(decision="decline", request_id="no")
    )
    assert declined.action and declined.action["status"] == "declined"
    assert declined.approval is None
    assert len(store.read().plans) == 1


def test_decision_http_rejects_other_session_and_legacy_route(
    store: RedisWorldStore,
) -> None:
    from fastapi.testclient import TestClient

    from moniepoint_demo.api import create_app

    service = SupportService(store)
    session = service.create_session()
    other = service.create_session()
    proposal = service.propose(session.id, ["transfer"], "p1")
    service.request_approval(session.id, proposal.id)
    with TestClient(
        create_app(store, agent_model=Model([answer("Applied.")]))
    ) as client:
        body = {"decision": "approve", "request_id": "approve-http"}
        assert (
            client.post(
                f"/support/sessions/{other.id}/proposals/{proposal.id}/decision",
                json=body,
            ).status_code
            == 404
        )
        route = f"/support/sessions/{session.id}/proposals/{proposal.id}/decision"
        assert (
            client.post(
                route, json=body, headers={"X-Demo-Customer-Id": "customer-002"}
            ).status_code
            == 404
        )
        assert (
            client.post(f"/presenter/approvals/{proposal.id}/confirm").status_code
            == 404
        )
        result = client.post(route, json=body)
        assert result.status_code == 200
        assert result.json()["action"]["status"] == "applied"
        assert client.post(route, json=body).json() == result.json()


def test_stale_decision_rejected_without_blocking_session(
    store: RedisWorldStore,
) -> None:
    from moniepoint_demo.support_agent import (
        DecisionRequest,
        MessageRequest,
        SupportAgent,
    )

    service = SupportService(store)
    first = service.create_session()
    proposal = service.propose(first.id, ["transfer"], "p1")
    service.request_approval(first.id, proposal.id)
    other = service.create_session()
    change = service.propose(other.id, ["airtime"], "p2")
    service.request_approval(other.id, change.id)
    service.decide(other.id, change.id, "approve", "d2")
    service.apply(other.id, change.id)
    runner = SupportAgent(store, Model([answer("We need a new proposal.")]))
    with pytest.raises(DomainError, match="changed"):
        runner.decide(
            first.id,
            proposal.id,
            DecisionRequest(decision="approve", request_id="stale"),
        )
    assert runner.inspect(first.id).active_request_id is None
    assert runner.ask(
        first.id, MessageRequest(message="What now?", request_id="followup")
    ).status in ("completed", "awaiting_approval")
    assert len(store.read().plans) == 2


def test_retry_after_apply_result_is_lost_does_not_apply_twice(
    store: RedisWorldStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from moniepoint_demo.support_agent import DecisionRequest, SupportAgent

    service = SupportService(store)
    session = service.create_session()
    proposal = service.propose(session.id, ["transfer"], "p1")
    service.request_approval(session.id, proposal.id)
    runner = SupportAgent(store, Model([]))
    apply = runner.service.apply

    def lose_result(session_id: str, proposal_id: str) -> Any:
        apply(session_id, proposal_id)
        raise DomainError("Lost result after commit", 503)

    monkeypatch.setattr(runner.service, "apply", lose_result)
    request = DecisionRequest(decision="approve", request_id="decision")
    assert runner.decide(session.id, proposal.id, request).status == "failed"
    assert len(store.read().plans) == 2
    recovered = SupportAgent(store, Model([answer("Applied.")])).decide(
        session.id, proposal.id, request
    )
    assert recovered.action and recovered.action["status"] == "applied"
    assert len(store.read().plans) == 2
    assert (
        len(SupportAgent(store, Model([])).inspect(session.id).application_events) == 1
    )


def test_memory_toggle_and_request_replay(store: RedisWorldStore) -> None:
    from moniepoint_demo.memory import MemoryHit, MemoryRecording, MemorySearch
    from moniepoint_demo.support_agent import MessageRequest, SupportAgent

    class Memory:
        searches = 0
        writes = 0

        def search(self, customer_id: str, query: str) -> MemorySearch:
            self.searches += 1
            return MemorySearch(
                status="ready",
                memories=[
                    MemoryHit(id="m1", text="Previously discussed ada-transfer-002.")
                ],
            )

        def record(self, *args: Any) -> MemoryRecording:
            self.writes += 1
            return MemoryRecording(status="recorded")

    memory = Memory()
    session = SupportService(store).create_session(use_memory=True)
    model = Model([answer("I will check the earlier transfer.")])
    runner = SupportAgent(store, model, memory=memory)  # type: ignore[arg-type]
    request = MessageRequest(message="That transfer again", request_id="m1")
    turn = runner.ask(session.id, request)
    assert "Previously discussed ada-transfer-002" in model.seen[0][0]["content"]
    assert turn.memory.status == "ready"
    assert turn.memory_recording.status == "recorded"
    runner.ask(session.id, request)
    assert (memory.searches, memory.writes) == (1, 1)
    other = SupportService(store).create_session(use_memory=False)
    runner = SupportAgent(store, Model([answer("Hello")]), memory=memory)  # type: ignore[arg-type]
    result = runner.ask(other.id, MessageRequest(message="Hi", request_id="off"))
    assert result.memory.status == "disabled"
    assert (memory.searches, memory.writes) == (1, 1)


def test_no_history_excludes_previous_turn_but_keeps_current_tools(
    store: RedisWorldStore,
) -> None:
    from moniepoint_demo.support_agent import MessageRequest, SupportAgent

    session = SupportService(store).create_session(use_history=False)
    model = Model(
        [
            answer("Your reference is BLUEBIRD."),
            call("get_savings_plan"),
            answer("Your current plan was checked."),
        ]
    )
    runner = SupportAgent(store, model)
    runner.ask(session.id, MessageRequest(message="Remember BLUEBIRD", request_id="1"))
    result = runner.ask(
        session.id, MessageRequest(message="Check my plan", request_id="2")
    )
    assert result.status == "completed"
    assert len(runner.inspect(session.id).turns) == 2
    assert all("BLUEBIRD" not in str(messages) for messages in model.seen[1:])
    assert model.seen[1][1:] == [{"role": "user", "content": "Check my plan"}]
    assert any(message["role"] == "tool" for message in model.seen[2])
    assert result.memory.status == "disabled"
