"""
Per-project Azure SDK credential/client cache for the ADF tools.

Every ADF tool call used to build a brand-new ClientSecretCredential +
DataFactoryManagementClient from scratch (mcp_servers/adf/tools/_shared.py's old _client()),
even for repeated calls within the same investigation/thread. Both are safe to reuse
concurrently:

  - azure-identity's in-memory token cache is documented safe for concurrent multi-thread use
    (TOKEN_CACHING.md: "the in-memory token cache provided by the Azure Identity library can be
    used by multiple threads concurrently").
  - DataFactoryManagementClient builds one shared ARMPipelineClient (HTTP connection pool) per
    instance and holds no other mutable per-call state (confirmed by reading
    azure/mgmt/datafactory/_client.py and _configuration.py) — the same "clients are
    thread-safe, treat as singletons" design Microsoft states explicitly for .NET
    (learn.microsoft.com/dotnet/azure/sdk/thread-safety). Python's own docs don't say this
    sentence for management-plane clients (see Azure/azure-sdk-for-python#28665, an open
    "missing docs" issue), so this half is inference from code + cross-language design parity,
    not a citable Python-specific guarantee.

None of that means Project A can't get Project B's cached client — the SDK has no concept of
"project" at all. That isolation is entirely on this cache: the key below binds every cached
client to the exact tenant/client/subscription/secret it was built from, so a correct lookup
can only ever return the client for that exact identity.

Called from both plain `def` tool functions (executed directly on a thread-pool worker via
RBACGateway._dispatch's run_in_executor) and from inside async tool functions' own
run_in_executor calls — i.e. genuinely concurrent OS threads, not just asyncio tasks sharing
one thread. Hence a real threading.Lock, not an asyncio.Lock.
"""
import hashlib
import threading
import time

from azure.mgmt.datafactory import DataFactoryManagementClient

from mcp_servers.adf.auth import get_credential

# Bounds how long a plaintext client_secret stays reachable in this process's memory via the
# cached ClientSecretCredential (see module docstring) — shortened from 90 to 30 min to narrow
# that exposure window; still long enough to keep a warm project's client alive across most of
# a chat session without rebuilding it on every call.
_TTL_SECONDS = 30 * 60

_lock = threading.Lock()
_entries: dict[tuple[str, str, str, str], "_Entry"] = {}


class _Entry:
    __slots__ = ("credential", "client", "created_at")

    def __init__(self, credential, client: DataFactoryManagementClient):
        self.credential = credential
        self.client = client
        self.created_at = time.monotonic()


def _cache_key(tenant_id: str, client_id: str, subscription_id: str, client_secret: str) -> tuple[str, str, str, str]:
    # Keying on project_id alone would be wrong: the same project's secret can rotate, or (in
    # principle) its tenant/subscription can change. Hashing the secret into the key means a
    # rotation naturally produces a cache miss and a fresh client — no separate "credential
    # version" bookkeeping needed. Truncated hash, not the raw secret, so the key itself never
    # holds recoverable secret material (e.g. if ever logged/repr'd).
    secret_fingerprint = hashlib.sha256(client_secret.encode()).hexdigest()[:16]
    return (tenant_id, client_id, subscription_id, secret_fingerprint)


def _evict_expired_locked() -> None:
    # ponytail: linear scan under the lock — fine at the entry counts this project will
    # realistically see (one entry per active project/secret-version); swap for a
    # background sweep if the project count ever makes this scan itself the bottleneck.
    now = time.monotonic()
    expired = [key for key, entry in _entries.items() if now - entry.created_at > _TTL_SECONDS]
    for key in expired:
        del _entries[key]


def get_client(tenant_id: str, client_id: str, client_secret: str, subscription_id: str) -> DataFactoryManagementClient:
    """Get-or-create the cached DataFactoryManagementClient for this exact project identity.

    Construction (ClientSecretCredential + DataFactoryManagementClient) does no network I/O —
    token acquisition happens lazily on the first real SDK call, not here — so holding the
    single process-wide lock across a cache miss is cheap and never blocks other projects on a
    slow Azure round-trip.
    """
    key = _cache_key(tenant_id, client_id, subscription_id, client_secret)
    with _lock:
        _evict_expired_locked()
        entry = _entries.get(key)
        if entry is None:
            credential = get_credential(tenant_id, client_id, client_secret)
            client = DataFactoryManagementClient(credential, subscription_id)
            entry = _Entry(credential, client)
            _entries[key] = entry
        return entry.client


def invalidate(tenant_id: str, client_id: str, client_secret: str, subscription_id: str) -> None:
    """Drop one cached entry, e.g. after the SDK reports an authentication failure for it.
    Same argument order as get_client (tenant_id, client_id, client_secret, subscription_id)
    deliberately, so callers can't silently transpose client_secret/subscription_id between
    the two calls. Not force-wired into every ADF tool call site (that would touch 30+
    functions for a scenario the secret-hash key already covers in the common case — see
    module docstring); exposed here for callers that want to react to a live auth failure
    directly."""
    key = _cache_key(tenant_id, client_id, subscription_id, client_secret)
    with _lock:
        _entries.pop(key, None)
