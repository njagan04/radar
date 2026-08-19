from mcp_servers.adf.tools._shared import _client

# The only resource kind with no checkpoint/dispatch involvement at all: an integration
# runtime has no versionable "definition" to snapshot/rollback - it's either a managed
# compute node (Azure-SSIS) that's simply running/stopped, or a self-hosted Windows service
# on customer infrastructure the SDK can't even reach. No _KIND constant, no _checkpoints.py
# import, no entry in any of _dispatch.py's *_BY_KIND tables - both tools below register
# directly in tools/__init__.py's TOOL_REGISTRY, same as triggers.py's direct action tools.


def get_integration_runtime_status(
    integration_runtime_name: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """Read-only. Works for any integration runtime type (Azure, self-hosted, Azure-SSIS)."""
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    status = client.integration_runtimes.get_status(
        resource_group, factory_name, integration_runtime_name
    )
    props = status.properties
    return {
        "name": integration_runtime_name,
        "type": getattr(props, "type", None),
        "state": getattr(props, "state", None),
    }


def start_integration_runtime(
    integration_runtime_name: str,
    factory_name: str,
    subscription_id: str,
    resource_group: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    reason: str,
) -> dict:
    """
    Starts a stopped Azure-SSIS (ManagedReserved) integration runtime.

    Does NOT apply to self-hosted integration runtimes: a self-hosted IR is a Windows
    service on customer infrastructure with no remote-start API anywhere in this SDK.
    If get_integration_runtime_status shows a self-hosted IR as Offline/Limited, that is
    a human-only fix (someone must restart the on-prem service) — do not call this tool
    for that case, it will fail against the service.
    """
    client = _client(tenant_id, client_id, client_secret, subscription_id)
    poller = client.integration_runtimes.begin_start(
        resource_group, factory_name, integration_runtime_name
    )
    result = poller.result()
    return {
        "name": integration_runtime_name,
        "reason": reason,
        "state": getattr(result.properties, "state", None),
    }
