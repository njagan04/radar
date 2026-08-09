"""
Declarative spec table for the 68 distinct-named ADF tools exposed to the chat agent via
retrieval (see mcp_servers/adf/tool_search_tool.py's build_chat_tools).
Each spec has its own real `mcp_servers.adf.tools.TOOL_REGISTRY`/`rbac_permissions` entry
(2026-07-30 RBAC unification — see tools/__init__.py's own docstring) — the 8 generic
operation names (create_resource, update_resource_definition, ...) are only used internally
to identify which shared implementation a derived spec's thin wrapper binds to, they are not
themselves registry/rbac_permissions keys anymore. Full rationale in the
"Expose all ADF tools" plan (majestic-sniffing-liskov.md) and claude-desktop/
toolsearch_prototype/vtests/toolsearch_evaluation.md, which measured that distinct per-kind
tool names score meaningfully higher end-to-end tool-selection accuracy than generic
resource_type-parameterized ones.

The actual per-tool descriptions/JSON schemas live one file per resource kind in this package
(pipelines.py, datasets.py, linked_services.py, data_flows.py, triggers.py,
global_parameters.py, integration_runtimes.py), mirroring claude-desktop/mcp_adf/schemas/'s own
per-kind layout (and mcp_servers/adf/tools/'s file naming) rather than a cross-kind generic
loop. SPECS below is just their concatenation.

Descriptions are adapted from claude-desktop/mcp_adf/schemas/*.py's real per-tool text
(restored there in that repo's 66-tool revert, confirmed already well-disambiguated on
review) rather than the gpt-4o-tuned mock overrides used during the evaluation itself — one
exception: back_*_definition/forward_*_definition descriptions are written fully
self-contained per kind here, NOT with claude-desktop's "see back_pipeline_definition for the
full behavior" cross-reference. That shortcut is safe in claude-desktop (all 68 tools are
always visible together in one MCP session), but unsafe here: retrieval may select
back_dataset_definition for a turn without also selecting back_pipeline_definition, leaving
the cross-reference pointing at a tool the model can't see this turn.

Real underlying function signatures (verified via `inspect.signature` against
mcp_servers.adf.tools.*, not assumed) all take `db`/`project`/factory_name/subscription_id/
resource_group/tenant_id/client_id/client_secret in addition to the domain args below - every
one of those is gateway-injected (`gateway.rbac.infra_params()` + Redis-cached
`client_secret`, plus `db`/`project` injected by `RBACGateway._dispatch()`), never
agent-supplied, so none of them appear in `params_json_schema` here.
"""
from mcp_servers.adf.schemas.base import ADFToolSpec
from mcp_servers.adf.schemas import (
    data_flows, datasets, global_parameters, integration_runtimes, linked_services, pipelines, triggers,
)

SPECS: list[ADFToolSpec] = (
    pipelines.TOOLS
    + datasets.TOOLS
    + linked_services.TOOLS
    + data_flows.TOOLS
    + triggers.TOOLS
    + global_parameters.TOOLS
    + integration_runtimes.TOOLS
)
