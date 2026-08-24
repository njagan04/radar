"""
Tests for the SDK-native tool-approval pause/resume flow (the "Expose all ADF tools" plan).
Most tests are service/router-level against the real FastAPI app + in-memory sqlite, mocking
only chat.service.chat_agent.run_chat_turn/resume_chat_turn (the actual LLM/Agents-SDK call) —
same pattern as tests/test_chat_router.py. One test exercises llm.agent's own SDK-result
translation directly (_result_to_chat_turn_result), since router-level mocking of run_chat_turn
itself would never catch a bug in that translation.

The real RunState.to_json()/from_json() round-trip and the full pause->approve->resume->
execute mechanics were already validated live against the real Azure deployment during this
feature's implementation (real needs_approval tool, real interruption, real resume, real tool
execution) — not re-asserted here as an automated test, since deterministically mocking the
Agents SDK's model layer to reproduce that live behavior offline would need to reverse-engineer
SDK internals not worth the risk/maintenance for this pass.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from conftest import seed_watchtower_access
from sqlalchemy import select

from config.settings import settings
from db.models import AuditLog, ChatThread, Credential, ProjectMetadata, RBACPermission
from llm.agent import ChatTurnResult, _result_to_chat_turn_result

_NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567812345678")


def user_id_for(email: str) -> str:
    """Deterministic per-email UUID for tests — claim/approval authorization is keyed on this
    id now (chat/service.py), not email, so it must be stable across calls within one test the
    same way a real user's public.User.id would be."""
    return str(uuid.uuid5(_NAMESPACE, email))


def _auth_headers(email: str) -> dict:
    """Mints a real X-Radar-Assertion — deps.py has no unverified-header fallback (removed
    2026-07-29, spoofable-field auth bypass)."""
    token = jwt.encode(
        {"email": email, "id": user_id_for(email)},
        settings.radar_assertion_secret,
        algorithm="HS256",
    )
    return {"X-Radar-Assertion": token}


PROJECT = "acme"
USER = "alice@acme.com"
OTHER_USER = "bob@acme.com"


async def _seed_project(db_factory, users=(USER,)):
    async with db_factory() as db:
        db.add(ProjectMetadata(project=PROJECT, platform="adf"))
        db.add(
            Credential(
                project=PROJECT,
                resource_group="rg",
                factory_name="f",
                tenant_id="t",
                client_id="c",
                subscription_id="s",
            )
        )
        db.add(
            RBACPermission(
                tool_name="update_dataset_definition",
                allowed=True,
                requires_consent=True,
            )
        )
        await db.commit()
    for u in users:
        await seed_watchtower_access(db_factory, user_id_for(u), u, PROJECT)


async def _create_thread(db_factory, claimed_by=USER) -> int:
    async with db_factory() as db:
        thread = ChatThread(
            project=PROJECT,
            investigation_id=None,
            claimed_by_user_id=user_id_for(claimed_by) if claimed_by else None,
            created_at=datetime.now(UTC),
        )
        db.add(thread)
        await db.commit()
        await db.refresh(thread)
        return thread.thread_id


def _pending_approval_blob(age: timedelta = timedelta(minutes=1)) -> dict:
    return {
        "run_state": {"fake": "state"},
        "sdk_version": "0.18.0",
        "pending_tools": [
            {
                "tool_call_id": "call_1",
                "tool_name": "update_dataset_definition",
                "tool_arguments": {
                    "name": "Foo",
                    "reason": "test",
                    "definition": {"type": "AzureSqlTable"},
                },
            }
        ],
        "triggering_message": "update the Foo dataset's definition",
        "created_at": (datetime.now(UTC) - age).isoformat(),
    }


@pytest.mark.asyncio
async def test_pending_approval_returns_202_and_blocks_further_messages(
    chat_client, chat_db_factory
):
    await _seed_project(chat_db_factory)
    thread_id = await _create_thread(chat_db_factory)

    pending_result = ChatTurnResult(
        kind="pending_approval",
        pending_tools=[
            {
                "tool_call_id": "call_1",
                "tool_name": "update_dataset_definition",
                "tool_arguments": {},
            }
        ],
        run_state_json={"run_state": {"fake": "state"}, "sdk_version": "0.18.0"},
        triggering_message="update the Foo dataset",
    )
    with patch(
        "chat.service.chat_agent.run_chat_turn",
        new=AsyncMock(return_value=pending_result),
    ):
        r = await chat_client.post(
            f"/chat/threads/{thread_id}/messages",
            json={"content": "update the Foo dataset"},
            headers=_auth_headers(USER),
        )

    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "pending_approval"
    assert body["pending_tools"][0]["tool_name"] == "update_dataset_definition"

    async with chat_db_factory() as db:
        thread = await db.get(ChatThread, thread_id)
        assert thread.pending_tool_approval is not None
        assert (
            thread.pending_tool_approval["triggering_message"]
            == "update the Foo dataset"
        )

    # A second message while paused must 409, not silently start a new turn.
    r2 = await chat_client.post(
        f"/chat/threads/{thread_id}/messages",
        json={"content": "anything else"},
        headers=_auth_headers(USER),
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_resolve_requires_claimant(chat_client, chat_db_factory):
    await _seed_project(chat_db_factory, users=(USER, OTHER_USER))
    thread_id = await _create_thread(chat_db_factory, claimed_by=USER)
    async with chat_db_factory() as db:
        thread = await db.get(ChatThread, thread_id)
        thread.pending_tool_approval = _pending_approval_blob()
        await db.commit()

    r = await chat_client.post(
        f"/chat/threads/{thread_id}/tool-approvals/resolve",
        json={"decision": "approve"},
        headers=_auth_headers(OTHER_USER),
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_resolve_ttl_expiry_auto_denies_with_audit_row(
    chat_client, chat_db_factory
):
    await _seed_project(chat_db_factory)
    thread_id = await _create_thread(chat_db_factory)
    async with chat_db_factory() as db:
        thread = await db.get(ChatThread, thread_id)
        thread.pending_tool_approval = _pending_approval_blob(age=timedelta(minutes=91))
        await db.commit()

    r = await chat_client.post(
        f"/chat/threads/{thread_id}/tool-approvals/resolve",
        json={"decision": "approve"},
        headers=_auth_headers(USER),
    )
    assert r.status_code == 410

    async with chat_db_factory() as db:
        thread = await db.get(ChatThread, thread_id)
        assert thread.pending_tool_approval is None

        audit_result = await db.execute(
            select(AuditLog).where(AuditLog.event_type == "tool_approval_expired")
        )
        rows = list(audit_result.scalars().all())
    assert len(rows) == 1
    assert rows[0].user_id == user_id_for(USER)


@pytest.mark.asyncio
async def test_resolve_hard_denies_when_tool_no_longer_allowed(
    chat_client, chat_db_factory
):
    await _seed_project(chat_db_factory)
    thread_id = await _create_thread(chat_db_factory)
    async with chat_db_factory() as db:
        thread = await db.get(ChatThread, thread_id)
        thread.pending_tool_approval = _pending_approval_blob()
        # Revoke the underlying permission after the turn already paused.
        result = await db.execute(
            select(RBACPermission).where(
                RBACPermission.tool_name == "update_dataset_definition"
            )
        )
        row = result.scalar_one()
        row.allowed = False
        await db.commit()

    r = await chat_client.post(
        f"/chat/threads/{thread_id}/tool-approvals/resolve",
        json={"decision": "approve"},
        headers=_auth_headers(USER),
    )
    assert r.status_code == 409

    async with chat_db_factory() as db:
        thread = await db.get(ChatThread, thread_id)
        assert thread.pending_tool_approval is None
        audit_result = await db.execute(
            select(AuditLog).where(
                AuditLog.event_type == "tool_approval_denied_revoked"
            )
        )
        assert len(list(audit_result.scalars().all())) == 1


@pytest.mark.asyncio
async def test_resolve_approve_completes_turn_and_persists_message(
    chat_client, chat_db_factory
):
    await _seed_project(chat_db_factory)
    thread_id = await _create_thread(chat_db_factory)
    async with chat_db_factory() as db:
        thread = await db.get(ChatThread, thread_id)
        thread.pending_tool_approval = _pending_approval_blob()
        await db.commit()

    completed = ChatTurnResult(
        kind="reply",
        reply_text="Done — updated.",
        tool_calls=[
            {
                "name": "update_dataset_definition",
                "call_id": "call_1",
                "status": "approved",
            }
        ],
    )
    with patch(
        "chat.service.chat_agent.resume_chat_turn",
        new=AsyncMock(return_value=completed),
    ):
        r = await chat_client.post(
            f"/chat/threads/{thread_id}/tool-approvals/resolve",
            json={"decision": "approve"},
            headers=_auth_headers(USER),
        )

    assert r.status_code == 200
    assert r.json()["content"] == "Done — updated."

    async with chat_db_factory() as db:
        thread = await db.get(ChatThread, thread_id)
        assert thread.pending_tool_approval is None


@pytest.mark.asyncio
async def test_resolve_deny_writes_audit_row_and_completes(
    chat_client, chat_db_factory
):
    await _seed_project(chat_db_factory)
    thread_id = await _create_thread(chat_db_factory)
    async with chat_db_factory() as db:
        thread = await db.get(ChatThread, thread_id)
        thread.pending_tool_approval = _pending_approval_blob()
        await db.commit()

    completed = ChatTurnResult(
        kind="reply", reply_text="Okay, not doing that.", tool_calls=[]
    )
    with patch(
        "chat.service.chat_agent.resume_chat_turn",
        new=AsyncMock(return_value=completed),
    ):
        r = await chat_client.post(
            f"/chat/threads/{thread_id}/tool-approvals/resolve",
            json={"decision": "deny", "rejection_message": "not now"},
            headers=_auth_headers(USER),
        )

    assert r.status_code == 200
    async with chat_db_factory() as db:
        audit_result = await db.execute(
            select(AuditLog).where(AuditLog.event_type == "tool_approval_denied")
        )
        rows = list(audit_result.scalars().all())
    assert len(rows) == 1
    assert rows[0].detail["rejection_message"] == "not now"


@pytest.mark.asyncio
async def test_resolve_can_repause_on_a_second_interruption(
    chat_client, chat_db_factory
):
    await _seed_project(chat_db_factory)
    thread_id = await _create_thread(chat_db_factory)
    async with chat_db_factory() as db:
        thread = await db.get(ChatThread, thread_id)
        thread.pending_tool_approval = _pending_approval_blob()
        await db.commit()

    still_pending = ChatTurnResult(
        kind="pending_approval",
        pending_tools=[
            {
                "tool_call_id": "call_2",
                "tool_name": "update_dataset_definition",
                "tool_arguments": {},
            }
        ],
        run_state_json={"run_state": {"fake": "state-2"}, "sdk_version": "0.18.0"},
        triggering_message="update the Foo dataset",
    )
    with patch(
        "chat.service.chat_agent.resume_chat_turn",
        new=AsyncMock(return_value=still_pending),
    ):
        r = await chat_client.post(
            f"/chat/threads/{thread_id}/tool-approvals/resolve",
            json={"decision": "approve"},
            headers=_auth_headers(USER),
        )

    assert r.status_code == 202
    async with chat_db_factory() as db:
        thread = await db.get(ChatThread, thread_id)
        assert (
            thread.pending_tool_approval["pending_tools"][0]["tool_call_id"] == "call_2"
        )


def test_result_to_chat_turn_result_translates_interruptions():
    """Exercises llm.agent's SDK-result-to-ChatTurnResult translation directly, faking just
    the shape of a real Runner.run() RunResult (interruptions/to_state/new_items/final_output)
    — router-level mocking of run_chat_turn itself would never catch a bug in this translation."""

    class _FakeRawItem:
        call_id = "call_abc"
        name = "update_dataset_definition"
        arguments = '{"name": "Foo", "reason": "test"}'

    class _FakeApprovalItem:
        raw_item = _FakeRawItem()
        tool_name = "update_dataset_definition"

    class _FakeRunState:
        def to_json(self):
            return {"fake": "state"}

    class _FakeResult:
        interruptions = [_FakeApprovalItem()]
        new_items = []
        final_output = None
        raw_responses = []

        def to_state(self):
            return _FakeRunState()

    ctr = _result_to_chat_turn_result(
        _FakeResult(), triggering_message="update the Foo dataset"
    )
    assert ctr.kind == "pending_approval"
    assert ctr.pending_tools == [
        {
            "tool_call_id": "call_abc",
            "tool_name": "update_dataset_definition",
            "tool_arguments": {"name": "Foo", "reason": "test"},
        }
    ]
    assert ctr.run_state_json["run_state"] == {"fake": "state"}
    assert ctr.triggering_message == "update the Foo dataset"


def test_result_to_chat_turn_result_translates_a_completed_reply():
    class _FakeToolCallRawItem:
        name = "get_pipeline_definition"

    class _FakeToolCallItem:
        type = "tool_call_item"
        raw_item = _FakeToolCallRawItem()

    class _FakeResult:
        interruptions = []
        new_items = [_FakeToolCallItem()]
        final_output = "Here's the answer."
        raw_responses = []

    ctr = _result_to_chat_turn_result(
        _FakeResult(), triggering_message="why did it fail"
    )
    assert ctr.kind == "reply"
    assert ctr.reply_text == "Here's the answer."
    assert ctr.tool_calls == [
        {"name": "get_pipeline_definition", "call_id": None, "status": "ran"}
    ]
