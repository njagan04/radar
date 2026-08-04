from db.models import ProjectRCA
from chat.seed_message import build_seed_message, extract_error_code


def test_extract_error_code_from_leaf():
    assert extract_error_code({"leaf": {"error_code": "UserErrorInvalidCredentials"}}) == "UserErrorInvalidCredentials"


def test_extract_error_code_flat_fallback():
    assert extract_error_code({"error_code": "Y"}) == "Y"


def test_extract_error_code_none_when_missing():
    assert extract_error_code(None) is None
    assert extract_error_code({}) is None


def test_extract_error_code_prefers_message_embedded_code_over_generic_numeric_field():
    """Real ADF shape (2026-08-06 bug): error_code is a generic numeric bucket ("2200") reused
    across unrelated failure types; the message text's own "ErrorCode=<Name>" names the
    SPECIFIC failure — and is what record_diagnosis_outcome actually gets keyed by when the LLM
    diagnoses it in chat. Extracting the numeric field instead meant this lookup could never
    find an already-diagnosed row for the exact same failure."""
    error_detail = {
        "error_code": "2200",
        "message": (
            "ErrorCode=MappingColumnNameNotFoundInSourceFile,'Type=Microsoft.DataTransfer."
            "Common.Shared.HybridDeliveryException,Message=Column 'ID' specified in column "
            "mapping cannot be found in 'mcp_test/customers.csv' source file.,"
            "Source=Microsoft.DataTransfer.ClientLibrary,'"
        ),
    }
    assert extract_error_code(error_detail) == "MappingColumnNameNotFoundInSourceFile"


def test_extract_error_code_message_pattern_checked_in_leaf_too():
    error_detail = {"leaf": {"error_code": "2200", "message": "ErrorCode=SqlFailedToConnect,..."}}
    assert extract_error_code(error_detail) == "SqlFailedToConnect"


def test_extract_error_code_falls_back_to_numeric_field_when_message_has_no_pattern():
    assert extract_error_code({"error_code": "2200", "message": "no ErrorCode token here"}) == "2200"


def test_build_seed_message_no_matching_rca():
    text = build_seed_message("PL_X", "acme", "Failed", "boom", matching_rca=None)
    assert "PL_X" in text
    assert "acme" in text
    assert "boom" in text
    assert "No prior recorded fix" in text


def test_build_seed_message_with_matching_rca():
    rca = ProjectRCA(
        pipeline_id="PL_X", project="acme", error_signature="s", error_category="c",
        fix_applied="restart the linked service", failure_count=3,
    )
    text = build_seed_message("PL_X", "acme", "Failed", "boom", matching_rca=rca)
    assert "3 time(s)" in text
    assert "restart the linked service" in text
