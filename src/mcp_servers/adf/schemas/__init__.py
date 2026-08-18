"""
Declarative spec table for the distinct-named ADF tools exposed to the chat agent via
retrieval (see mcp_servers/adf/tool_search_tool.py's build_chat_tools).

Each spec has its own `mcp_servers.adf.tools.TOOL_REGISTRY`/`rbac_permissions` entry. The 4
generic operation names (create_resource, update_resource_definition, list_resources,
get_resource_definition_raw) are used internally to identify which shared implementation a
derived spec's thin wrapper binds to; they are not themselves registry/rbac_permissions keys.

The actual per-tool descriptions/JSON schemas live one file per resource kind in this package
(pipelines.py, datasets.py, linked_services.py, data_flows.py, triggers.py,
global_parameters.py, integration_runtimes.py). SPECS below is their concatenation.

Underlying tool functions all take factory_name/subscription_id/resource_group/tenant_id/
client_id/client_secret in addition to the domain args below — every one of those is
gateway-injected (`gateway.rbac.infra_params()` + Redis-cached `client_secret`), never
agent-supplied, so none of them appear in `params_json_schema` here.
"""

from mcp_servers.adf.schemas import (
    data_flows,
    datasets,
    global_parameters,
    integration_runtimes,
    linked_services,
    pipelines,
    triggers,
)
from mcp_servers.adf.schemas.base import ADFToolSpec

SPECS: list[ADFToolSpec] = (
    pipelines.TOOLS
    + datasets.TOOLS
    + linked_services.TOOLS
    + data_flows.TOOLS
    + triggers.TOOLS
    + global_parameters.TOOLS
    + integration_runtimes.TOOLS
)
