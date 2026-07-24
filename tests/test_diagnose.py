from unittest.mock import AsyncMock

import pytest

from db.models import Investigation, ProjectFactory
from workflow import diagnose


def _make_investigation(**overrides) -> Investigation:
    defaults = dict(
        investigation_id="inv-1",
        project="acme",
        platform="adf",
        pipeline_name="PL_TEST",
        run_status="Failed",
        start_time="2026-01-01T00:00:00Z",
        end_time=None,
        last_error="boom",
        error_detail=None,
        failure_count=1,
        trigger_type="ScheduleTrigger",
        status="pending_diagnosis",
        diagnosis_result=None,
    )
    defaults.update(overrides)
    return Investigation(**defaults)


def _make_project_factory(**overrides) -> ProjectFactory:
    defaults = dict(
        project="acme",
        resource_group="rg",
        factory_name="f",
        tenant_id="t",
        client_id="c",
        subscription_id="s",
        key_vault_uri="https://acme-vault.vault.azure.net/",
    )
    defaults.update(overrides)
    return ProjectFactory(**defaults)


class _FakeScalars:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _FakeExecuteResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return _FakeScalars(self._value)


class _FakeDb:
    """investigation via db.get(Investigation, ...); factory via
    select(ProjectFactory)...scalars().first() — matches diagnose.py's actual query shapes."""

    def __init__(self, investigation, factory):
        self._investigation = investigation
        self._factory = factory
        self.commit = AsyncMock()

    async def get(self, model_cls, _pk):
        assert model_cls is Investigation
        return self._investigation

    async def execute(self, _stmt):
        return _FakeExecuteResult(self._factory)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _db_factory(investigation, factory):
    def factory_fn():
        return _FakeDb(investigation, factory)
    return factory_fn


class _FakeSemaphore:
    """Simple async-context-manager test double — tracks enter/exit without depending on
    DistributedSemaphore's real Redis-backed internals."""

    def __init__(self):
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, *exc):
        self.exited += 1
        return False


@pytest.fixture
def patched_nodes(monkeypatch):
    """Patch every workflow node run_diagnosis calls with an AsyncMock, so tests assert
    on ROUTING (which nodes ran) rather than depending on real LLM/Azure calls.

    Matches the current (post classifier/dependency_check retirement) flow:
      pre_check -> (cancelled: skip) | (not cancelled: load_context -> investigator) -> notifier
    """
    mocks = {
        "initial_evidence_fetch": AsyncMock(return_value={}),
        "pre_check": AsyncMock(return_value={}),  # {} == not cancelled
        "load_context": AsyncMock(return_value={"prior_rca_context": None}),
        "investigator": AsyncMock(return_value={
            "rca_id": 1, "investigation_summary": "done",
            "error_category": "unknown", "requires_human_action": False,
        }),
        "notifier": AsyncMock(return_value={
            "notify_sent": True, "needs_approval": True, "proposed_at": "2026-01-01T00:00:00Z",
        }),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(f"workflow.diagnose.{name}", mock)
    return mocks


@pytest.mark.asyncio
async def test_cancelled_short_circuits_before_investigator(patched_nodes):
    patched_nodes["pre_check"].return_value = {"error_category": "cancelled"}
    db_factory = _db_factory(_make_investigation(), _make_project_factory())

    state = await diagnose.run_diagnosis("inv-1", db_factory, redis=AsyncMock())

    patched_nodes["load_context"].assert_not_called()
    patched_nodes["investigator"].assert_not_called()
    patched_nodes["notifier"].assert_called_once()
    assert state["error_category"] == "cancelled"


@pytest.mark.asyncio
async def test_not_cancelled_runs_load_context_and_investigator(patched_nodes):
    db_factory = _db_factory(_make_investigation(), _make_project_factory())

    state = await diagnose.run_diagnosis("inv-1", db_factory, redis=AsyncMock())

    patched_nodes["load_context"].assert_called_once()
    patched_nodes["investigator"].assert_called_once()
    patched_nodes["notifier"].assert_called_once()
    assert state["rca_id"] == 1


@pytest.mark.asyncio
async def test_requires_human_action_still_runs_investigator_first(patched_nodes):
    """requires_human_action is now determined BY the investigator (from its own
    error_category output), not a pre-chat classifier — so investigator always runs unless
    cancelled; there's no separate skip branch for it anymore."""
    patched_nodes["investigator"].return_value = {
        "rca_id": None, "investigation_summary": "needs a human",
        "error_category": "credential_expired", "requires_human_action": True,
    }
    db_factory = _db_factory(_make_investigation(), _make_project_factory())

    state = await diagnose.run_diagnosis("inv-1", db_factory, redis=AsyncMock())

    patched_nodes["investigator"].assert_called_once()
    assert state["requires_human_action"] is True


@pytest.mark.asyncio
async def test_run_diagnosis_persists_status_and_result(patched_nodes):
    investigation = _make_investigation()
    fake_db = _FakeDb(investigation, _make_project_factory())
    db_factory = lambda: fake_db

    await diagnose.run_diagnosis("inv-1", db_factory, redis=AsyncMock())

    assert investigation.status == "diagnosed"
    assert investigation.diagnosis_result["rca_id"] == 1
    assert investigation.diagnosis_result["needs_approval"] is True
    assert investigation.diagnosis_result["error_category"] == "unknown"


@pytest.mark.asyncio
async def test_run_diagnosis_raises_for_unknown_investigation(patched_nodes):
    db_factory = _db_factory(None, _make_project_factory())
    with pytest.raises(ValueError, match="No investigation found"):
        await diagnose.run_diagnosis("missing", db_factory, redis=AsyncMock())


@pytest.mark.asyncio
async def test_run_diagnosis_respects_semaphore(patched_nodes):
    semaphore = _FakeSemaphore()
    db_factory = _db_factory(_make_investigation(), _make_project_factory())

    state = await diagnose.run_diagnosis("inv-1", db_factory, redis=AsyncMock(), semaphore=semaphore)

    assert state is not None
    assert semaphore.entered == 1
    assert semaphore.exited == 1
