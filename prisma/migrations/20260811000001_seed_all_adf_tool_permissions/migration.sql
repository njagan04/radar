-- Only 5 of the 68 real ADF tool_name values (mcp_servers/adf/schemas SPECS /
-- mcp_servers/adf/tools TOOL_REGISTRY) had an rbac_permissions row before this migration —
-- every other tool was silently invisible to the chat agent (tool_search_tool.py's
-- allowed_specs filters out any spec with no matching row at all, not just denied ones).
-- README's "known, tracked, not yet fixed" item, deferred until the ADF tool port landed —
-- it has (66/68-tool retrieval is already live), so this is that fix.
--
-- allowed=true for all of them (these are the real, intended tool set). requires_consent is
-- tiered by whether the underlying operation mutates live ADF infrastructure or a pipeline/
-- trigger run: create/update/rollback/back/forward/rerun/cancel/start/stop = true; get/list
-- (read-only) = false. This same column also drives the OpenAI Agents SDK's own
-- FunctionTool(needs_approval=...) (tool_search_tool.py:build_chat_tools), so getting this
-- right determines which tool calls actually pause for human approval, not just RBAC's own
-- allow/deny check.
--
-- ON CONFLICT DO UPDATE (not DO NOTHING) so this migration also corrects the 5 pre-existing
-- rows if their allowed/requires_consent values had drifted, rather than only adding the
-- missing 63.
INSERT INTO "rbac_permissions" ("tool_name", "allowed", "requires_consent", "platform") VALUES
    ('back_data_flow_definition', true, true, 'adf'),
    ('back_dataset_definition', true, true, 'adf'),
    ('back_global_parameter_definition', true, true, 'adf'),
    ('back_linked_service_definition', true, true, 'adf'),
    ('back_pipeline_definition', true, true, 'adf'),
    ('back_trigger_definition', true, true, 'adf'),
    ('cancel_pipeline_run', true, true, 'adf'),
    ('cancel_trigger_run', true, true, 'adf'),
    ('create_data_flow', true, true, 'adf'),
    ('create_dataset', true, true, 'adf'),
    ('create_global_parameter', true, true, 'adf'),
    ('create_linked_service', true, true, 'adf'),
    ('create_pipeline', true, true, 'adf'),
    ('create_trigger', true, true, 'adf'),
    ('forward_data_flow_definition', true, true, 'adf'),
    ('forward_dataset_definition', true, true, 'adf'),
    ('forward_global_parameter_definition', true, true, 'adf'),
    ('forward_linked_service_definition', true, true, 'adf'),
    ('forward_pipeline_definition', true, true, 'adf'),
    ('forward_trigger_definition', true, true, 'adf'),
    ('get_activity_run_error', true, false, 'adf'),
    ('get_activity_run_history', true, false, 'adf'),
    ('get_activity_run_io', true, false, 'adf'),
    ('get_data_flow_definition', true, false, 'adf'),
    ('get_data_flow_definition_raw', true, false, 'adf'),
    ('get_dataset_definition', true, false, 'adf'),
    ('get_dataset_definition_raw', true, false, 'adf'),
    ('get_global_parameter_definition_raw', true, false, 'adf'),
    ('get_integration_runtime_status', true, false, 'adf'),
    ('get_linked_service', true, false, 'adf'),
    ('get_linked_service_definition_raw', true, false, 'adf'),
    ('get_pipeline_definition', true, false, 'adf'),
    ('get_pipeline_definition_raw', true, false, 'adf'),
    ('get_pipeline_run_history', true, false, 'adf'),
    ('get_pipeline_run_status', true, false, 'adf'),
    ('get_trigger', true, false, 'adf'),
    ('get_trigger_run_history', true, false, 'adf'),
    ('list_activity_runs', true, false, 'adf'),
    ('list_data_flow_snapshots', true, false, 'adf'),
    ('list_data_flows', true, false, 'adf'),
    ('list_dataset_snapshots', true, false, 'adf'),
    ('list_datasets', true, false, 'adf'),
    ('list_global_parameter_snapshots', true, false, 'adf'),
    ('list_global_parameters', true, false, 'adf'),
    ('list_linked_service_snapshots', true, false, 'adf'),
    ('list_linked_services', true, false, 'adf'),
    ('list_pipeline_runs', true, false, 'adf'),
    ('list_pipeline_snapshots', true, false, 'adf'),
    ('list_pipelines', true, false, 'adf'),
    ('list_trigger_snapshots', true, false, 'adf'),
    ('list_triggers', true, false, 'adf'),
    ('rerun_pipeline', true, true, 'adf'),
    ('rerun_trigger_run', true, true, 'adf'),
    ('rollback_data_flow_definition', true, true, 'adf'),
    ('rollback_dataset_definition', true, true, 'adf'),
    ('rollback_global_parameter_definition', true, true, 'adf'),
    ('rollback_linked_service_definition', true, true, 'adf'),
    ('rollback_pipeline_definition', true, true, 'adf'),
    ('rollback_trigger_definition', true, true, 'adf'),
    ('start_integration_runtime', true, true, 'adf'),
    ('start_trigger', true, true, 'adf'),
    ('stop_trigger', true, true, 'adf'),
    ('update_data_flow_definition', true, true, 'adf'),
    ('update_dataset_definition', true, true, 'adf'),
    ('update_global_parameter_definition', true, true, 'adf'),
    ('update_linked_service_definition', true, true, 'adf'),
    ('update_pipeline_definition', true, true, 'adf'),
    ('update_trigger_definition', true, true, 'adf')
ON CONFLICT ("tool_name") DO UPDATE SET
    "allowed" = EXCLUDED."allowed",
    "requires_consent" = EXCLUDED."requires_consent",
    "platform" = EXCLUDED."platform";
