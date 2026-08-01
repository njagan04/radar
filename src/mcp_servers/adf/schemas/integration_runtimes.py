"""Integration-runtime tool specs — mirrors claude-desktop/mcp_adf/schemas/
integration_runtimes.py's per-kind layout. No generic-dispatch operations here: an IR has no
versionable definition (no create/update/rollback/checkpoint concept applies), just
running/stopped runtime state — so both tools are always-distinct, registry_tool_name == name."""
from mcp_servers.adf.schemas.base import ADFToolSpec, REASON_PROP, schema

TOOLS = [
    ADFToolSpec(
        name="get_integration_runtime_status",
        registry_tool_name="get_integration_runtime_status",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Get an integration runtime's state (works for Azure, self-hosted, and Azure-SSIS IR "
            "types). Use this before start_integration_runtime to check whether starting it is even "
            "applicable."
        ),
        params_json_schema=schema({"integration_runtime_name": {"type": "string"}}, ["integration_runtime_name"]),
    ),
    ADFToolSpec(
        name="start_integration_runtime",
        registry_tool_name="start_integration_runtime",
        resource_type=None,
        name_kwarg=None,
        description=(
            "Starts a stopped Azure-SSIS (managed) integration runtime. Does NOT work on self-hosted "
            "IRs — no remote-start API exists; that's human-only. Only call this after "
            "get_integration_runtime_status confirms the IR is a managed type and is Stopped."
        ),
        params_json_schema=schema({
            "integration_runtime_name": {"type": "string"},
            "reason": REASON_PROP,
        }, ["integration_runtime_name", "reason"]),
    ),
]
