import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from mcp_servers.adf import client_cache


class _FakeCredential:
    def __init__(self, tenant_id, client_id, client_secret):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret


class _FakeClient:
    construct_count = 0
    construct_delay = 0.0

    def __init__(self, credential, subscription_id):
        if _FakeClient.construct_delay:
            time.sleep(_FakeClient.construct_delay)
        self.credential = credential
        self.subscription_id = subscription_id
        _FakeClient.construct_count += 1


@pytest.fixture(autouse=True)
def _patch_sdk(monkeypatch):
    monkeypatch.setattr(client_cache, "get_credential", _FakeCredential)
    monkeypatch.setattr(client_cache, "DataFactoryManagementClient", _FakeClient)
    client_cache._entries.clear()
    _FakeClient.construct_count = 0
    _FakeClient.construct_delay = 0.0
    yield
    client_cache._entries.clear()


# Test 1 — basic project isolation
def test_different_projects_get_different_clients_with_correct_identity():
    a = client_cache.get_client("tenant-a", "client-a", "secret-a", "sub-a")
    b = client_cache.get_client("tenant-b", "client-b", "secret-b", "sub-b")

    assert a is not b
    assert a.credential.tenant_id == "tenant-a"
    assert a.subscription_id == "sub-a"
    assert b.credential.tenant_id == "tenant-b"
    assert b.subscription_id == "sub-b"


def test_same_project_reuses_cached_client():
    a1 = client_cache.get_client("tenant-a", "client-a", "secret-a", "sub-a")
    a2 = client_cache.get_client("tenant-a", "client-a", "secret-a", "sub-a")

    assert a1 is a2
    assert _FakeClient.construct_count == 1


# Test 2 — concurrent different projects, 10 calls per project, interleaved across threads
def test_concurrent_different_projects_never_cross_contaminate():
    projects = [
        ("tenant-a", "client-a", "secret-a", "sub-a"),
        ("tenant-b", "client-b", "secret-b", "sub-b"),
        ("tenant-c", "client-c", "secret-c", "sub-c"),
    ]
    mismatches = []
    lock = threading.Lock()

    def worker(params):
        tenant_id, client_id, secret, sub = params
        for _ in range(20):
            client = client_cache.get_client(tenant_id, client_id, secret, sub)
            if (
                client.credential.tenant_id != tenant_id
                or client.subscription_id != sub
            ):
                with lock:
                    mismatches.append(
                        (tenant_id, client.credential.tenant_id, client.subscription_id)
                    )

    # 5 threads per project, interleaved start order so the pool genuinely races them
    tasks = projects * 5
    with ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        list(ex.map(worker, tasks))

    assert mismatches == []
    # one cached client per distinct project, no duplicates from the race
    assert _FakeClient.construct_count == len(projects)


# Test 3 — concurrent same project, 10 simultaneous calls safely share one client
def test_concurrent_same_project_reuses_single_client():
    def worker(_):
        return client_cache.get_client("tenant-a", "client-a", "secret-a", "sub-a")

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(worker, range(10)))

    assert all(r is results[0] for r in results)
    assert _FakeClient.construct_count == 1


# Test 4 — cache initialization race: many concurrent first-time callers, one construction
def test_cache_initialization_race_constructs_exactly_once():
    _FakeClient.construct_delay = (
        0.05  # widen the race window around the cold-cache path
    )
    barrier = threading.Barrier(12)

    def worker(_):
        barrier.wait()
        return client_cache.get_client("tenant-x", "client-x", "secret-x", "sub-x")

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(worker, range(12)))

    assert _FakeClient.construct_count == 1
    assert all(r is results[0] for r in results)


# Test 5 — credential rotation produces a new client, isolated from the old one
def test_credential_rotation_creates_new_client_not_old_one():
    old = client_cache.get_client("tenant-a", "client-a", "secret-v1", "sub-a")
    new = client_cache.get_client("tenant-a", "client-a", "secret-v2", "sub-a")

    assert old is not new
    assert new.credential.client_secret == "secret-v2"
    assert _FakeClient.construct_count == 2
    # both entries live until TTL/explicit invalidation — rotation doesn't retroactively
    # corrupt or evict the old one out from under any in-flight call still using it
    assert len(client_cache._entries) == 2


# Test 6 (rotation/TTL) — expired entries are evicted and rebuilt on next access
def test_ttl_expiry_evicts_stale_entry_and_reconstructs(monkeypatch):
    fake_now = [1_000.0]
    monkeypatch.setattr(client_cache.time, "monotonic", lambda: fake_now[0])

    first = client_cache.get_client("tenant-a", "client-a", "secret-a", "sub-a")
    fake_now[0] += client_cache._TTL_SECONDS + 1
    second = client_cache.get_client("tenant-a", "client-a", "secret-a", "sub-a")

    assert first is not second
    assert _FakeClient.construct_count == 2
    assert (
        len(client_cache._entries) == 1
    )  # expired entry swept, not left to accumulate


# Test 7 — explicit invalidation forces reconstruction (e.g. after an auth failure)
def test_invalidate_forces_reconstruction():
    first = client_cache.get_client("tenant-a", "client-a", "secret-a", "sub-a")
    client_cache.invalidate("tenant-a", "client-a", "secret-a", "sub-a")
    second = client_cache.get_client("tenant-a", "client-a", "secret-a", "sub-a")

    assert first is not second
    assert _FakeClient.construct_count == 2


def test_invalidate_unknown_key_is_a_no_op():
    client_cache.invalidate(
        "no-such-tenant", "no-such-client", "no-such-secret", "no-such-sub"
    )
    assert client_cache._entries == {}
