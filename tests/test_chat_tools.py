"""
Tests for llm.tools — the platform-agnostic half of tool-building: the two generic RCA tools
(check_known_fix, record_diagnosis_outcome) and build_tools_for_platform, the dispatcher that
adds each platform's own tool set on top. ADF-specific tool-building behavior (retrieval,
rbac_permissions filtering, needs_approval) is covered separately in test_tool_search_tool.py —
split out (2026-07-28) when llm/tools.py's ADF-specific half moved to
mcp_servers/adf/tool_search_tool.py, so this file no longer needs to seed the full ADF
rbac_permissions table just to exercise the two generic tools.
"""

import json

import pytest
from agents.tool_context import ToolContext
from sqlalchemy import select

from db.models import ProjectRCA, RBACPermission
from llm.context import WorkflowContext
from llm.tools import build_tools_for_platform
from mcp_servers.adf.schemas import SPECS

_TOOL_CTX = ToolContext(
    context=None,
    tool_name="record_diagnosis_outcome",
    tool_call_id="test",
    tool_arguments="{}",
)

_STATE = {
    "investigation_id": None,
    "thread_id": None,
    "pipeline_name": "orders_pipeline",
    "project": "acme",
    "platform": "adf",
}


async def _seed_all_rbac_rows(db_factory):
    """build_tools_for_platform("adf", ...) still calls through to mcp_servers.adf.tool_search_tool,
    which queries rbac_permissions for every registered ADF tool — seed it so that call
    succeeds, even though these tests only care about the generic tools it returns alongside."""
    async with db_factory() as db:
        for registry_tool_name in {spec.registry_tool_name for spec in SPECS}:
            db.add(
                RBACPermission(
                    tool_name=registry_tool_name, allowed=True, requires_consent=False
                )
            )
        await db.commit()


@pytest.mark.asyncio
async def test_custom_lookup_tools_always_present_regardless_of_platform(
    chat_db_factory,
):
    ctx = WorkflowContext(db_factory=chat_db_factory, redis=None)

    tools = await build_tools_for_platform(
        "unknown_future_platform",
        _STATE,
        ctx,
        "some question",
        user_id="alice@acme.com",
    )
    names = [t.name for t in tools]
    assert names == [
        "check_known_fix",
        "record_diagnosis_outcome",
    ]  # no platform tools, generic ones still present


@pytest.mark.asyncio
async def test_custom_lookup_tools_present_alongside_adf_tools(chat_db_factory):
    await _seed_all_rbac_rows(chat_db_factory)
    ctx = WorkflowContext(db_factory=chat_db_factory, redis=None)

    tools = await build_tools_for_platform(
        "adf", _STATE, ctx, "some unrelated question", user_id="alice@acme.com"
    )
    names = [t.name for t in tools]
    assert "check_known_fix" in names
    assert "record_diagnosis_outcome" in names
    assert len(names) > 2  # ADF tools also present


def _record_tool(tools):
    return next(t for t in tools if t.name == "record_diagnosis_outcome")


@pytest.mark.asyncio
async def test_record_diagnosis_outcome_creates_then_updates_same_row(chat_db_factory):
    ctx = WorkflowContext(db_factory=chat_db_factory, redis=None)
    tools = await build_tools_for_platform(
        "unknown_future_platform", _STATE, ctx, "q", user_id="alice@acme.com"
    )
    record_tool = _record_tool(tools)

    first = json.loads(
        await record_tool.on_invoke_tool(
            _TOOL_CTX,
            json.dumps(
                {
                    "error_signature": "conn_timeout_sql01",
                    "error_category": "timeout",
                    "root_cause": "SQL01 connection pool exhausted",
                }
            ),
        )
    )
    assert first["created"] is True

    second = json.loads(
        await record_tool.on_invoke_tool(
            _TOOL_CTX,
            json.dumps(
                {
                    "error_signature": "conn_timeout_sql01",
                    "error_category": "timeout",
                    "fix_applied": "Increased pool size to 50",
                }
            ),
        )
    )
    assert second["created"] is False

    async with chat_db_factory() as db:
        result = await db.execute(
            select(ProjectRCA).where(
                ProjectRCA.pipeline_id == _STATE["pipeline_name"],
                ProjectRCA.project == _STATE["project"],
                ProjectRCA.error_signature == "conn_timeout_sql01",
            )
        )
        rows = list(result.scalars().all())

    assert len(rows) == 1  # updated in place, not duplicated
    assert rows[0].failure_count == 2
    assert (
        rows[0].root_cause == "SQL01 connection pool exhausted"
    )  # preserved from first call
    assert rows[0].fix_applied == "Increased pool size to 50"  # added by second call


@pytest.mark.asyncio
async def test_record_diagnosis_outcome_different_signature_creates_separate_row(
    chat_db_factory,
):
    ctx = WorkflowContext(db_factory=chat_db_factory, redis=None)
    tools = await build_tools_for_platform(
        "unknown_future_platform", _STATE, ctx, "q", user_id="alice@acme.com"
    )
    record_tool = _record_tool(tools)

    await record_tool.on_invoke_tool(
        _TOOL_CTX,
        json.dumps(
            {
                "error_signature": "conn_timeout_sql01",
                "error_category": "timeout",
            }
        ),
    )
    await record_tool.on_invoke_tool(
        _TOOL_CTX,
        json.dumps(
            {
                "error_signature": "bad_json_payload",
                "error_category": "data_quality",
            }
        ),
    )

    async with chat_db_factory() as db:
        result = await db.execute(
            select(ProjectRCA).where(
                ProjectRCA.pipeline_id == _STATE["pipeline_name"],
                ProjectRCA.project == _STATE["project"],
            )
        )
        rows = list(result.scalars().all())

    assert len(rows) == 2
    assert {r.error_signature for r in rows} == {
        "conn_timeout_sql01",
        "bad_json_payload",
    }
