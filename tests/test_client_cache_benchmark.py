"""
Performance comparison: build-a-fresh-client-per-call (the old behavior) vs. the client_cache.

What this can and can't measure locally, honestly:
  - Object construction (ClientSecretCredential + DataFactoryManagementClient __init__) does no
    network I/O, so its own cost is sub-millisecond either way — not the real win.
  - The real win eliminated by caching is that a BRAND NEW ClientSecretCredential starts with an
    empty token cache, so the old per-call code path forced a genuine AAD token-endpoint network
    round trip (typically ~100-500ms) on every single ADF tool call. Reusing one credential
    instance means only the first call per project pays that cost; every later call is served
    from azure-identity's in-memory token cache (documented safe for concurrent use — see
    client_cache.py's module docstring) until the token's own ~60-90min expiry.
  - This test can't make a real AAD call (no creds, no network in CI), so the AAD round trip is
    stood in for with an explicit, clearly-labeled synthetic delay on first construction — not a
    live measurement of Azure's actual latency, just a way to show the shape of the win: N calls
    pay the round trip once (cached) vs. N times (uncached).
"""

import time

from mcp_servers.adf import client_cache

# Stand-in for a real AAD token-endpoint round trip. Real-world figures for this vary by
# region/load; this is a conservative placeholder purely to make the comparison's shape visible,
# not a claim about actual Azure latency.
_SIMULATED_AAD_ROUND_TRIP_SECONDS = 0.15
_CALLS_PER_PROJECT = 20


class _FakeCredential:
    def __init__(self, tenant_id, client_id, client_secret):
        self.tenant_id, self.client_id, self.client_secret = (
            tenant_id,
            client_id,
            client_secret,
        )


def _make_fake_client_cls():
    class _FakeClient:
        construct_count = 0

        def __init__(self, credential, subscription_id):
            time.sleep(
                _SIMULATED_AAD_ROUND_TRIP_SECONDS
            )  # only the FIRST build per project pays this
            self.credential, self.subscription_id = credential, subscription_id
            _FakeClient.construct_count += 1

    return _FakeClient


def test_cached_reuse_is_faster_than_rebuilding_per_call(monkeypatch, capsys):
    fake_client_cls = _make_fake_client_cls()
    monkeypatch.setattr(client_cache, "get_credential", _FakeCredential)
    monkeypatch.setattr(client_cache, "DataFactoryManagementClient", fake_client_cls)
    client_cache._entries.clear()

    # Uncached baseline: construct fresh every call (the old _shared.py._client() behavior).
    start = time.perf_counter()
    for _ in range(_CALLS_PER_PROJECT):
        _FakeCredential("tenant-a", "client-a", "secret-a")
        fake_client_cls(_FakeCredential("tenant-a", "client-a", "secret-a"), "sub-a")
    uncached_seconds = time.perf_counter() - start

    # Cached: one project, _CALLS_PER_PROJECT tool calls.
    fake_client_cls.construct_count = 0
    start = time.perf_counter()
    for _ in range(_CALLS_PER_PROJECT):
        client_cache.get_client("tenant-a", "client-a", "secret-a", "sub-a")
    cached_seconds = time.perf_counter() - start

    assert (
        fake_client_cls.construct_count == 1
    )  # only the first call actually built anything
    assert cached_seconds < uncached_seconds / 5  # order-of-magnitude win, not a fluke

    capsys.readouterr()  # discard; real output goes via -s below
    print(
        f"\n{_CALLS_PER_PROJECT} tool calls, one project, "
        f"{_SIMULATED_AAD_ROUND_TRIP_SECONDS * 1000:.0f}ms simulated AAD round trip per uncached build:\n"
        f"  uncached (old behavior): {uncached_seconds:.3f}s ({_CALLS_PER_PROJECT} client+credential builds)\n"
        f"  cached (client_cache):   {cached_seconds:.3f}s (1 build, {_CALLS_PER_PROJECT - 1} cache hits)\n"
    )
