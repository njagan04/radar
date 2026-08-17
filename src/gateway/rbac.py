import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from azure.core.exceptions import ClientAuthenticationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AuditLog, RBACPermission
from gateway.credential_resolution import resolve_client_secret
from llm.investigation_state import InvestigationState
from mcp_servers.adf import client_cache, tools as adf_tools

logger = logging.getLogger(__name__)

_TOOL_REGISTRY: dict[str, Callable[..., dict]] = adf_tools.TOOL_REGISTRY  # type: ignore[assignment]


async def set_platform_context(db, platform: str) -> None:
    """Sets the session GUC the rbac_permissions RLS policy checks against. set_config(...,
    true) is Postgres's parameter-bindable equivalent of `SET LOCAL` (plain SET doesn't accept
    bind parameters, only a literal), and is transaction-scoped, so it clears itself at the
    end of this session's transaction.

    Best-effort, deliberately swallows failures: sqlite (the test fixture) has no set_config
    at all, and some hand-rolled test doubles don't accept a params dict — neither should
    block the actual enforcement (_check_permission), which runs regardless.
    """
    try:
        await db.execute(
            text("SELECT set_config('app.current_platform', :platform, true)"),
            {"platform": platform},
        )
    except Exception:
        logger.debug(
            "set_platform_context: skipped (unsupported by this session)", exc_info=True
        )


def infra_params(state: "dict | InvestigationState") -> dict:
    """Extract the non-secret factory identifiers (sourced from Credential) from state."""
    return {
        "tenant_id": state.get("tenant_id"),
        "client_id": state.get("client_id"),
        "subscription_id": state.get("subscription_id"),
        "resource_group": state.get("resource_group"),
        "factory_name": state.get("factory_name"),
    }


class RBACGateway:
    def __init__(
        self,
        db: AsyncSession,
        infra_params: dict | None = None,
        investigation_id: str | None = None,
        thread_id: str | None = None,
    ):
        """
        `investigation_id` is used for AuditLog rows and may be `None` — ad-hoc chat tool
        calls have no FailureEvent to point at, so that's a real, expected value here, not a
        missing one.

        `thread_id` is passed explicitly rather than derived from anything else — every
        failure-triggered chat call needs its own real thread_id on the AuditLog row, same as
        an ad-hoc call.
        """
        self._db = db
        self._infra_params = infra_params or {}
        self._investigation_id = investigation_id
        self._thread_id = thread_id

    async def call(
        self,
        tool_name: str,
        arguments: dict,
        user_id: str | None,
        pipeline_id: str,
        project: str,
        platform: str,
    ) -> dict:
        # No role dimension — permission is per-tool only (allowed / requires_consent).
        # requires_consent tiering is enforced by the OpenAI Agents SDK's native tool-approval
        # mechanism (FunctionTool(needs_approval=...), see mcp_servers/adf/tool_search_tool.py's
        # build_chat_tools) before this is ever called for a chat-agent tool; this check is an
        # independent, approval-mechanism-agnostic gate (defense in depth).
        #
        # _check_permission is platform-scoped directly rather than relying solely on the
        # rbac_permissions RLS policy, which is a no-op since the app connects as a Postgres
        # superuser (unconditionally bypasses RLS) — without this, a tool_name collision
        # between two platforms would let this call read the wrong platform's
        # allowed/requires_consent row. set_platform_context stays too, as the second,
        # DB-level layer for once the connection role is fixed.
        await self._set_platform_context(platform)
        allowed = await self._check_permission(tool_name, platform)
        event_type = "rbac_tool_call_allowed" if allowed else "rbac_tool_call_denied"
        # `arguments` (caller-supplied only, never the enriched dict with client_secret) is
        # logged here so the audit trail can distinguish which resource/kind a call touched —
        # logged before _enrich() runs, so no secret is ever included.
        await self._log(
            event_type,
            pipeline_id,
            project,
            platform,
            user_id,
            {"tool": tool_name, "arguments": arguments},
        )
        if not allowed:
            raise PermissionError(f"'{tool_name}' is not an allowed tool")
        enriched = await self._enrich(arguments, project)
        return await self._dispatch(tool_name, enriched)

    async def _set_platform_context(self, platform: str) -> None:
        await set_platform_context(self._db, platform)

    async def _check_permission(self, tool_name: str, platform: str) -> bool:
        result = await self._db.execute(
            select(RBACPermission.allowed).where(
                RBACPermission.tool_name == tool_name,
                RBACPermission.platform == platform,
            )
        )
        row = result.scalar_one_or_none()
        return bool(row) if row is not None else False

    async def _enrich(self, arguments: dict, project: str) -> dict:
        # Non-secret infra params come from state (passed at construction).
        # client_secret is resolved fresh from public."Credential" on every call — decrypting
        # it is a local AES operation, no network I/O. The expensive part, the Azure SDK
        # client itself, is cached separately (see mcp_servers/adf/client_cache.py).
        secret = await resolve_client_secret(self._db, project)
        # infra_params and client_secret are spread AFTER arguments so a model-supplied key
        # (tool schemas don't declare tenant_id/client_id/subscription_id/resource_group/
        # factory_name/client_secret, but strict_json_schema=False means nothing stops a model
        # from emitting one anyway) can never override the trusted project identity resolved
        # from server-side state — arguments must lose any name collision, not win it.
        return {**arguments, **self._infra_params, "client_secret": secret}

    async def _dispatch(self, tool_name: str, arguments: dict) -> dict:
        fn = _TOOL_REGISTRY.get(tool_name)
        if fn is None:
            raise ValueError(f"No tool registered for '{tool_name}'")
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: fn(**arguments))
        except ClientAuthenticationError:
            # The cached client/credential for this exact identity (mcp_servers/adf/
            # client_cache.py) is no longer valid — evict it so the next call rebuilds fresh.
            # Not a retry of this call: an auth failure means the request never reached Azure,
            # so there's no partial side effect to worry about, but retrying automatically here
            # would still risk masking a genuinely bad secret as a transient hiccup. The model's
            # own duplicate-call handling (tool_search_tool.py) decides whether to try again,
            # now against a freshly-built client.
            client_cache.invalidate(
                arguments["tenant_id"],
                arguments["client_id"],
                arguments["client_secret"],
                arguments["subscription_id"],
            )
            raise

    async def _log(
        self,
        event_type: str,
        pipeline_id: str,
        project: str,
        platform: str,
        user_id: str | None,
        detail: dict | None,
    ) -> None:
        self._db.add(
            AuditLog(
                investigation_id=self._investigation_id,
                thread_id=self._thread_id,
                pipeline_name=pipeline_id,
                project=project,
                platform=platform,
                timestamp=datetime.now(UTC),
                event_type=event_type,
                user_id=user_id,
                detail=detail,
            )
        )
        await self._db.commit()


async def call_tool(
    db: AsyncSession,
    *,
    tool_name: str,
    arguments: dict,
    user_id: str | None,
    pipeline_id: str,
    project: str,
    platform: str,
    infra_params_dict: dict,
    investigation_id: str | None,
    thread_id: str | None = None,
) -> dict:
    """The one call site mcp_servers/adf/tool_search_tool.py's _call_gateway should use.
    Runs RBACGateway in-process — radar is deployed as a single FastAPI service."""
    gateway = RBACGateway(
        db=db,
        infra_params=infra_params_dict,
        investigation_id=investigation_id,
        thread_id=thread_id,
    )
    return await gateway.call(
        tool_name=tool_name,
        arguments=arguments,
        user_id=user_id,
        pipeline_id=pipeline_id,
        project=project,
        platform=platform,
    )
