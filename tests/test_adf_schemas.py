"""
Unit tests for mcp_servers.adf.schemas (the 44-distinct-tool declarative spec table, one file
per resource kind) and mcp_servers.adf.tool_search_tool (the keyword-based tool selector) — see the
"Expose all ADF tools" plan and claude-desktop/toolsearch_prototype/vtests/
toolsearch_evaluation.md for the live evaluation this design is based on. These are
structural/spot-check tests, not a re-run of the full 57-case live evaluation (already done,
real Azure calls, documented separately).

The git-like checkpoint/rollback/back/forward versioning system (list_*_snapshots,
rollback_*_definition, back_*_definition, forward_*_definition — 24 tool names across 6
resource kinds) was removed 2026-08-12, shrinking the spec table from 68 to 44.
"""

import re
from pathlib import Path

from mcp_servers.adf.schemas import SPECS
from mcp_servers.adf.tool_search_tool import (
    build_keyword_index,
    retrieve_relevant_tools,
)
from mcp_servers.adf.tools import TOOL_REGISTRY

_RBAC_SEED_MIGRATION = (
    Path(__file__).parent.parent
    / "prisma"
    / "migrations"
    / "20260811000001_seed_all_adf_tool_permissions"
    / "migration.sql"
)
_RBAC_VERSIONING_REMOVAL_MIGRATION = (
    Path(__file__).parent.parent
    / "prisma"
    / "migrations"
    / "20260812000000_remove_resource_versioning"
    / "migration.sql"
)


def test_all_spec_names_are_unique():
    names = [spec.name for spec in SPECS]
    assert len(names) == 44
    assert len(set(names)) == 44


def test_every_spec_name_exists_in_the_real_tool_registry():
    """Catches a typo'd/missing mapping immediately rather than at runtime — a spec whose name
    isn't a real TOOL_REGISTRY key would silently 500 the first time a chat turn tried to call
    it. TOOL_REGISTRY is keyed by spec.name (the 44 distinct, LLM-facing names), not
    registry_tool_name, since the RBAC unification (2026-07-30, see tools/__init__.py's own
    docstring) — registry_tool_name now only identifies which of the 4 shared generic
    implementations a derived spec's wrapper binds to, not a rbac_permissions/TOOL_REGISTRY key."""
    referenced = {spec.name for spec in SPECS}
    assert referenced == set(TOOL_REGISTRY.keys())
    assert len(TOOL_REGISTRY) == 44


def test_generic_derived_specs_carry_resource_type_and_name_kwarg():
    update_dataset = next(
        spec for spec in SPECS if spec.name == "update_dataset_definition"
    )
    assert update_dataset.registry_tool_name == "update_resource_definition"
    assert update_dataset.resource_type == "dataset"
    assert update_dataset.name_kwarg == "dataset_name"


def test_direct_specs_have_no_resource_type_or_name_kwarg_remapping():
    cancel_pipeline_run = next(
        spec for spec in SPECS if spec.name == "cancel_pipeline_run"
    )
    assert cancel_pipeline_run.registry_tool_name == "cancel_pipeline_run"
    assert cancel_pipeline_run.resource_type is None
    assert cancel_pipeline_run.name_kwarg is None


def test_list_resources_derived_specs_take_no_name_argument():
    """list_resources has no per-resource "name" concept (factory-wide sweep) - unlike every
    other generic-derived operation."""
    list_datasets = next(spec for spec in SPECS if spec.name == "list_datasets")
    assert list_datasets.registry_tool_name == "list_resources"
    assert list_datasets.name_kwarg is None
    assert list_datasets.params_json_schema["properties"] == {}
    assert list_datasets.params_json_schema["required"] == []


def test_get_resource_definition_raw_excludes_trigger():
    names = {
        spec.name
        for spec in SPECS
        if spec.registry_tool_name == "get_resource_definition_raw"
    }
    assert "get_trigger_definition_raw" not in names
    assert names == {
        "get_pipeline_definition_raw",
        "get_dataset_definition_raw",
        "get_linked_service_definition_raw",
        "get_data_flow_definition_raw",
        "get_global_parameter_definition_raw",
    }


def test_every_spec_has_a_seeded_rbac_permissions_row():
    """Regression test for the real bug this guards against: only 5 of 68 tool_name values ever
    had an rbac_permissions row, so tool_search_tool.py's allowed_specs filter silently made 63
    tools invisible in chat (a spec with no matching row is dropped entirely, not just denied).
    Parses the seed migration's literal tool_name values, then subtracts the 24 checkpoint-
    versioning tool names the later 20260812000000 migration deletes (SPECS shrank to 44 in the
    same change) — rather than re-deriving the current set from scratch, so this fails loudly if
    a new tool is ever added to SPECS without a matching seed row."""
    seed_sql = _RBAC_SEED_MIGRATION.read_text()
    seeded_names = set(
        re.findall(
            r"^\s*\('([a-z_]+)', (?:true|false), (?:true|false), 'adf'\),?$",
            seed_sql,
            re.MULTILINE,
        )
    )

    removal_sql = _RBAC_VERSIONING_REMOVAL_MIGRATION.read_text()
    removed_names = set(re.findall(r"'([a-z_]+)'", removal_sql))

    current_seeded_names = seeded_names - removed_names
    spec_names = {spec.name for spec in SPECS}
    assert current_seeded_names == spec_names, (
        f"missing from seed: {spec_names - current_seeded_names}; "
        f"seeded but no longer a real spec: {current_seeded_names - spec_names}"
    )


def test_retrieval_finds_the_expected_tool_for_representative_messages():
    """Spot-check, not a re-run of the full 57-case live evaluation (already done against the
    real model, see claude-desktop/toolsearch_prototype/vtests/toolsearch_evaluation.md) -
    this just confirms the ported algorithm still works against the real spec table."""
    index = build_keyword_index(SPECS)
    always_include = {"list_pipelines", "get_pipeline_definition"}

    cases = [
        (
            "Can you update the Foo dataset's definition to fix the schema drift?",
            "update_dataset_definition",
        ),
        ("that trigger run is hanging, cancel it", "cancel_trigger_run"),
        ("what datasets exist in this data factory", "list_datasets"),
        ("kill the pipeline run that's stuck", "cancel_pipeline_run"),
    ]
    for message, expected in cases:
        selected = retrieve_relevant_tools(
            message, SPECS, index, top_k=8, always_include=always_include
        )
        selected_names = [spec.name for spec in selected]
        assert expected in selected_names, (
            f"{expected!r} not retrieved for {message!r}: got {selected_names}"
        )
