"""
Shared helpers for ADF resource serialization and validation. These work around real Azure
SDK serialization quirks — do not "simplify" this file.
"""

import copy
import re
from datetime import timedelta, timezone

import azure.mgmt.datafactory.models as _adf_models
from azure.mgmt.datafactory import DataFactoryManagementClient

from mcp_servers.adf import client_cache

_IST = timezone(timedelta(hours=5, minutes=30))


def _client(
    tenant_id: str, client_id: str, client_secret: str, subscription_id: str
) -> DataFactoryManagementClient:
    return client_cache.get_client(tenant_id, client_id, client_secret, subscription_id)


def _to_ist(value) -> str | None:
    """Converts a UTC timestamp to an IST-labeled string, matching ADF Studio's Monitor UI display."""
    if value is None:
        return None
    return value.astimezone(_IST).strftime("%Y-%m-%d %H:%M:%S IST")


def _to_wire_dict(resource) -> dict:
    """
    Returns the true wire-format dict (camelCase, e.g. "dependsOn",
    "typeProperties.waitTimeInSeconds") — the shape *Resource.deserialize() expects and the
    same shape ADF Studio's "Code" view / ARM export uses.

    Not the same as `.as_dict()`, which uses Python attribute names (snake_case) and is
    unsafe to feed back into `.deserialize()`: any field whose wire name differs from its
    Python attribute name silently vanishes into an inert `additional_properties` bucket
    instead of the real attribute.
    """
    serialized = resource.serialize(keep_readonly=True)
    return serialized.get("properties", serialized)


def _find_miscased_fields(obj, path: str = "") -> list[str]:
    """
    Recursively walks a deserialized SDK model object tree looking for
    `additional_properties` keys that are a miscased (snake_case) version of a real
    attribute the model has under a different, correctly-cased name: such a key is silently
    dropped by `Model.deserialize()` instead of raising, so the field never reaches ADF, with
    no error to signal it. Returns human-readable warnings; empty list if nothing looks wrong.

    Only flags a key that exactly matches a real Python attribute name this object type has
    (e.g. "depends_on", "wait_time_in_seconds") — ADF genuinely allows arbitrary custom
    properties on activities, so a generic `additional_properties` entry alone is not flagged.
    """
    warnings: list[str] = []
    if obj is None:
        return warnings
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            warnings.extend(_find_miscased_fields(item, f"{path}[{i}]"))
        return warnings
    if isinstance(obj, dict):
        for k, v in obj.items():
            warnings.extend(_find_miscased_fields(v, f"{path}.{k}" if path else k))
        return warnings

    attribute_map = getattr(obj, "_attribute_map", None)
    if attribute_map is None:
        return warnings

    extra = getattr(obj, "additional_properties", None) or {}
    real_names = set(attribute_map.keys()) - {"additional_properties"}
    for key, extra_value in extra.items():
        if key not in real_names or not extra_value:
            continue
        # Some compound wire keys get flattened into the real attribute by msrest's
        # deserializer AND redundantly echoed into additional_properties — a benign echo,
        # not dropped data. If the real attribute is populated, skip; otherwise it's a
        # genuine drop.
        if getattr(obj, key, None):
            continue
        correct_wire_key = attribute_map[key]["key"]
        warnings.append(
            f'{path or "root"}: key "{key}" was silently ignored — it looks like a '
            f'miscased version of the real field (wire key "{correct_wire_key}"; ADF '
            f"uses camelCase, not snake_case). This value will NOT be applied."
        )

    for attr_name in real_names:
        if attr_name == "additional_properties":
            continue
        try:
            value = getattr(obj, attr_name)
        except AttributeError:
            continue
        warnings.extend(
            _find_miscased_fields(value, f"{path}.{attr_name}" if path else attr_name)
        )
    return warnings


def _reject_if_miscased(resource, resource_label: str) -> dict | None:
    """
    Returns an error dict if the just-deserialized resource has any miscased-field warnings,
    else None. Call this right after `Model.deserialize()` and before the mutating API call.
    """
    warnings = _find_miscased_fields(resource)
    if not warnings:
        return None
    return {
        "error": "possible_miscased_fields",
        "resource": resource_label,
        "warnings": warnings,
        "hint": 'ADF wire format is camelCase (e.g. "dependsOn", "typeProperties.waitTimeInSeconds"), '
        "not Python-style snake_case. Fix the flagged keys and retry — nothing was written to ADF.",
    }


def _to_snake_case(name: str) -> str:
    """camelCase/PascalCase -> snake_case (e.g. "dataFlow" -> "data_flow")."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _resolve_model_class(type_str: str):
    """
    Maps an msrest attribute type string (e.g. "DataFlowReference", "[Activity]",
    "{ParameterSpecification}") to the actual model class in
    azure.mgmt.datafactory.models, or None if it's a primitive/dict-of-primitive with
    nothing to recurse into.
    """
    if not type_str:
        return None
    if type_str.startswith("[") and type_str.endswith("]"):
        type_str = type_str[1:-1]
    if type_str.startswith("{") and type_str.endswith("}"):
        type_str = type_str[1:-1]
    if type_str in (
        "str",
        "int",
        "float",
        "bool",
        "object",
        "long",
        "date",
        "datetime",
        "duration",
    ):
        return None
    return getattr(_adf_models, type_str, None)


def _resolve_concrete_class(base_cls, raw_item: dict):
    """
    Uses the SDK's own polymorphic deserializer to find which concrete subclass `raw_item`
    resolves to under `base_cls` (e.g. Activity -> ExecuteDataFlowActivity based on its
    "type" field), consistent with what the real top-level deserialize() call does.

    `Model.deserialize()` mutates its input dict (msrest pops consumed keys, including the
    discriminator "type" key, as it parses). `raw_item` is a piece of the caller's actual
    definition dict, the same object later fed to the real `Model.deserialize()` call — the
    deepcopy here prevents this speculative classification call from stripping "type" off
    that shared object before the real deserialize runs.
    """
    try:
        return type(base_cls.deserialize(copy.deepcopy(raw_item)))
    except Exception:
        return base_cls


def _expected_keys(model_cls) -> tuple[dict, dict]:
    """
    Returns (flattened, plain) for a model class's _attribute_map:
      flattened: {namespace: {correct_subkey: (attr_name, type_str)}}  — for dotted wire
                 keys like "typeProperties.dataFlow" or "properties.activities"
      plain:     {correct_key: (attr_name, type_str)}                 — for flat wire keys
    """
    flattened: dict[str, dict[str, tuple[str, str]]] = {}
    plain: dict[str, tuple[str, str]] = {}
    for attr_name, meta in getattr(model_cls, "_attribute_map", {}).items():
        if attr_name == "additional_properties":
            continue
        key = meta["key"]
        type_str = meta["type"]
        if "." in key:
            ns, sub = key.split(".", 1)
            flattened.setdefault(ns, {})[sub.split(".")[0]] = (attr_name, type_str)
        else:
            plain[key] = (attr_name, type_str)
    return flattened, plain


def _recurse_into_value(raw_val, type_str: str, path: str) -> list[str]:
    if type_str.startswith("[") and type_str.endswith("]"):
        # list-of-X, e.g. "[Activity]" — each item is one X.
        if not isinstance(raw_val, list):
            return []
        base_cls = _resolve_model_class(type_str)
        if base_cls is None:
            return []
        warnings: list[str] = []
        for i, item in enumerate(raw_val):
            if not isinstance(item, dict):
                continue
            concrete_cls = _resolve_concrete_class(base_cls, item)
            warnings.extend(
                _find_dropped_flattened_fields(
                    item, concrete_cls, f"{path}[{i}]", is_top=False
                )
            )
        return warnings
    if type_str.startswith("{") and type_str.endswith("}"):
        # dict-of-X, e.g. "{GlobalParameterSpecification}", "{ParameterSpecification}" —
        # raw_val is a mapping of arbitrary caller-chosen names to X-shaped values, NOT
        # itself one X (distinct from a plain object field of the same model type).
        if not isinstance(raw_val, dict):
            return []
        base_cls = _resolve_model_class(type_str)
        if base_cls is None:
            return []
        warnings = []
        for sub_key, sub_val in raw_val.items():
            if not isinstance(sub_val, dict):
                continue
            concrete_cls = _resolve_concrete_class(base_cls, sub_val)
            warnings.extend(
                _find_dropped_flattened_fields(
                    sub_val, concrete_cls, f"{path}.{sub_key}", is_top=False
                )
            )
        return warnings
    if isinstance(raw_val, dict):
        base_cls = _resolve_model_class(type_str)
        if base_cls is None:
            return []
        concrete_cls = _resolve_concrete_class(base_cls, raw_val)
        return _find_dropped_flattened_fields(raw_val, concrete_cls, path, is_top=False)
    return []


def _find_dropped_flattened_fields(
    raw: dict, model_cls, path: str = "", is_top: bool = True
) -> list[str]:
    """
    Walks the raw (pre-deserialization) input dict against `model_cls`'s _attribute_map,
    catching a class of silent data loss `_find_miscased_fields` cannot see: msrest's
    "client flattening" for compound wire keys (e.g. Activity's "typeProperties.dataFlow",
    PipelineResource's "properties.activities"). A miscased nested key (e.g.
    "typeProperties": {"data_flow": ...} instead of {"dataFlow": ...}) just vanishes during
    the flattening walk instead of landing in `additional_properties`, so this compares the
    raw dict directly against the expected wire-key shape before deserialization.

    Resolves polymorphic activity/dataset/linked-service/data-flow subtypes via the SDK's
    own `Model.deserialize()` (see `_resolve_concrete_class`) rather than hand-chasing
    `_subtype_map` discriminator chains.

    `is_top`: PipelineResource/DatasetResource/LinkedServiceResource/DataFlowResource/
    GlobalParameterResource support the SDK's client-flatten shorthand for their own
    top-level "properties" wrapper — callers may pass either the ARM-nested shape or the
    flat shape with "properties" omitted (this codebase always uses the flat shape). That
    shorthand applies only at the top level; nested namespaces like an activity's own
    "typeProperties" always require the literal key to be present.
    """
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return warnings

    flattened, plain = _expected_keys(model_cls)

    for ns, expected in flattened.items():
        ns_raw = raw.get(ns)
        if not isinstance(ns_raw, dict):
            if is_top and ns == "properties":
                ns_raw = raw  # client-flatten shorthand — see docstring
            else:
                continue
        for raw_key in ns_raw:
            if raw_key in expected:
                continue
            for exp_key, (attr_name, _type_str) in expected.items():
                if _to_snake_case(exp_key) == raw_key:
                    warnings.append(
                        f'{path or "root"}.{ns}: key "{raw_key}" looks like a miscased '
                        f'version of "{exp_key}" (python attribute "{attr_name}") — ADF '
                        f'wire format needs it nested as "{ns}.{exp_key}". This value will '
                        f"be silently dropped, not written."
                    )
                    break
        for exp_key, (_attr_name, type_str) in expected.items():
            sub_val = ns_raw.get(exp_key)
            if sub_val is not None:
                warnings.extend(
                    _recurse_into_value(sub_val, type_str, f"{path}.{ns}.{exp_key}")
                )

    for key, (_attr_name, type_str) in plain.items():
        raw_val = raw.get(key)
        if raw_val is not None:
            warnings.extend(
                _recurse_into_value(raw_val, type_str, f"{path}.{key}" if path else key)
            )

    return warnings


def _reject_if_dropped_fields(raw: dict, model_cls, resource_label: str) -> dict | None:
    """
    Returns an error dict if `raw` — the definition dict as supplied by the caller, before
    `Model.deserialize()` — has any dropped-flattened-field warnings, else None. Call this
    alongside `_reject_if_miscased`, but on the raw dict and before deserialization.
    """
    warnings = _find_dropped_flattened_fields(raw, model_cls)
    if not warnings:
        return None
    return {
        "error": "possible_dropped_nested_fields",
        "resource": resource_label,
        "warnings": warnings,
        "hint": 'ADF wire format nests type-specific fields under "typeProperties" (or, for the outer '
        'resource wrapper, "properties") using camelCase keys — e.g. "typeProperties.dataFlow", '
        'not "typeProperties.data_flow". Fix the flagged keys and retry — nothing was written to ADF.',
    }
