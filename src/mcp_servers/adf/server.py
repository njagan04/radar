import asyncio
import json

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from mcp_servers.adf import tools as adf_tools

server = Server("nexus-adf")

_TOOLS = [
    Tool(
        name="get_activity_run_error",
        description="Fetch activity-level error detail for the most recent failed run of a pipeline.",
        inputSchema={
            "type": "object",
            "properties": {
                "pipeline_name": {"type": "string"},
                "event_timestamp": {"type": "string", "description": "ISO-8601 timestamp from the failure event"},
            },
            "required": ["pipeline_name", "event_timestamp"],
        },
    ),
    Tool(
        name="get_pipeline_run_status",
        description="Get current status of a specific pipeline run (used for freshness check before rerun).",
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
            },
            "required": ["run_id"],
        },
    ),
    Tool(
        name="get_pipeline_run_history",
        description="Fetch recent run history for a pipeline.",
        inputSchema={
            "type": "object",
            "properties": {
                "pipeline_name": {"type": "string"},
                "days": {"type": "integer", "default": 7},
            },
            "required": ["pipeline_name"],
        },
    ),
    Tool(
        name="get_activity_run_history",
        description=(
            "Aggregated summary of which activities have failed in recent runs of a pipeline. "
            "Returns failure counts and last error code per activity — useful for spotting recurring failures."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pipeline_name": {"type": "string"},
                "days": {"type": "integer", "default": 7},
            },
            "required": ["pipeline_name"],
        },
    ),
    Tool(
        name="get_pipeline_definition",
        description="Fetch the pipeline definition (activity graph).",
        inputSchema={
            "type": "object",
            "properties": {
                "pipeline_name": {"type": "string"},
            },
            "required": ["pipeline_name"],
        },
    ),
    Tool(
        name="get_linked_service",
        description="Fetch connection details for a linked service.",
        inputSchema={
            "type": "object",
            "properties": {
                "service_name": {"type": "string"},
            },
            "required": ["service_name"],
        },
    ),
    Tool(
        name="rerun_pipeline",
        description="Trigger a new run of a pipeline. RBAC-gated: requires senior_eng or admin role.",
        inputSchema={
            "type": "object",
            "properties": {
                "pipeline_name": {"type": "string"},
                "parameters": {"type": "object"},
            },
            "required": ["pipeline_name"],
        },
    ),
]

_TOOL_MAP = adf_tools.TOOL_REGISTRY


@server.list_tools()
async def list_tools() -> list[Tool]:
    return _TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    fn = _TOOL_MAP.get(name)
    if fn is None:
        raise ValueError(f"Unknown tool: {name}")
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: fn(**arguments))
    return [TextContent(type="text", text=json.dumps(result))]


async def run():
    async with stdio_server() as streams:
        await server.run(*streams, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(run())
