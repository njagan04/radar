-- Removes the git-like per-resource checkpoint/rollback/back/forward versioning system
-- (mcp_servers/adf/tools/_checkpoints.py, deleted) — deliberately out of scope, per user
-- decision 2026-08-12: ADF's own Azure resources already carry their own publish/version
-- history, and the extra 24 tool names (list_*_snapshots/rollback_*/back_*/forward_* across
-- pipeline/dataset/linked_service/data_flow/trigger/global_parameter) were costing retrieval
-- accuracy for no real benefit. If this is ever needed again, see git history for
-- _checkpoints.py and this migration.

-- Drop child tables before the blob table they reference.
DROP TABLE IF EXISTS "resource_snapshot_cursor";
DROP TABLE IF EXISTS "resource_snapshots";
DROP TABLE IF EXISTS "resource_snapshot_blobs";

-- Remove the now-dead rbac_permissions rows for the 24 removed tool names — otherwise these
-- rows would just be permanently-unreachable dead weight (nothing dispatches on these names
-- anymore once the ADFToolSpec entries are gone from mcp_servers/adf/schemas/*.py).
DELETE FROM "rbac_permissions" WHERE "tool_name" IN (
    'back_data_flow_definition',
    'back_dataset_definition',
    'back_global_parameter_definition',
    'back_linked_service_definition',
    'back_pipeline_definition',
    'back_trigger_definition',
    'forward_data_flow_definition',
    'forward_dataset_definition',
    'forward_global_parameter_definition',
    'forward_linked_service_definition',
    'forward_pipeline_definition',
    'forward_trigger_definition',
    'list_data_flow_snapshots',
    'list_dataset_snapshots',
    'list_global_parameter_snapshots',
    'list_linked_service_snapshots',
    'list_pipeline_snapshots',
    'list_trigger_snapshots',
    'rollback_data_flow_definition',
    'rollback_dataset_definition',
    'rollback_global_parameter_definition',
    'rollback_linked_service_definition',
    'rollback_pipeline_definition',
    'rollback_trigger_definition'
);
