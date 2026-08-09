"""
build_tools_for_platform — the dispatcher that gives every chat thread the generic RCA tools
(check_known_fix, record_diagnosis_outcome, built in mcp_servers/rca/tools.py) plus whatever
tool set its platform registers on top. The ADF-specific tool set itself (68 distinct tools,
retrieval-selected) lives in mcp_servers/adf/tool_search_tool.py — this module must not import
anything ADF-specific, so that adding Synapse/Databricks/Fabric later only means registering
another builder below, not touching this file's own logic.
"""
import logging

from agents import set_default_openai_client, set_tracing_disabled
from openai import AsyncOpenAI

from config.settings import settings
from llm.context import WorkflowContext
from llm.investigation_state import InvestigationState

logger = logging.getLogger(__name__)

# Azure AI Foundry's v1 endpoint — native Responses API, no forced Chat Completions mode
# needed. set_default_openai_client/set_tracing_disabled are process-global SDK settings, set
# once here since llm/agent.py imports this module for build_tools_for_platform.
_client = AsyncOpenAI(
    base_url=settings.azure_openai_v1_base_url,
    api_key=settings.azure_openai_api_key,
)

set_default_openai_client(_client, use_for_tracing=False)
set_tracing_disabled(True)  # no OpenAI platform account to receive Agents SDK traces


# Platform dispatch — only "adf" is implemented today (the only platform with a real MCP tool
# server, mcp_servers/adf/), but llm/agent.py serves ad-hoc threads for ANY project regardless
# of ProjectMetadata.platform, so it must not hardcode ADF's tool set. Deliberately NOT
# building out a full multi-platform tool abstraction yet (Synapse/Databricks/Fabric don't
# have MCP servers of their own yet). Unregistered platforms get an empty tool list (graceful)
# rather than raising, so a chat thread on an unsupported platform can still hold a
# conversation, just without platform-specific tool access. Imported lazily (function-local,
# not top-level) so this module never has to import anything ADF-specific at module load time.
async def build_tools_for_platform(
    platform: str, state: InvestigationState, ctx: WorkflowContext, message: str, user_id: str | None,
) -> list:
    from mcp_servers.rca.tools import build_rca_tools
    custom_tools = build_rca_tools(state, ctx, user_id)

    if platform == "adf":
        from mcp_servers.adf.tool_search_tool import build_chat_tools
        platform_tools = await build_chat_tools(state, ctx, message, user_id)
    else:
        logger.warning(
            "No tool set registered for platform=%r — proceeding with only the generic RCA "
            "tools (only 'adf' is implemented today)", platform,
        )
        platform_tools = []

    return [*custom_tools, *platform_tools]
