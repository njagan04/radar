import asyncio
import json
from collections.abc import Callable
from datetime import datetime, timezone

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import AuditLog, RBACPermission
from mcp_servers.adf import tools as adf_tools
from workflow.state import InvestigationState

_TOOL_REGISTRY: dict[str, Callable[..., dict]] = adf_tools.TOOL_REGISTRY  # type: ignore[assignment]


def infra_params(state: "dict | InvestigationState") -> dict:
    """Extract the non-secret factory identifiers (sourced from ProjectFactory) from state."""
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
        redis: aioredis.Redis,
        investigation_id: str,
        infra_params: dict | None = None,
    ):
        self._db = db
        self._redis = redis
        self._investigation_id = investigation_id
        self._infra_params = infra_params or {}

    async def call(
        self,
        tool_name: str,
        arguments: dict,
        actor: str,
        pipeline_id: str,
        project: str,
        platform: str,
    ) -> dict:
        # No role dimension — permission is per-tool only (allowed / requires_consent).
        # requires_consent tiering is enforced by the chat UI showing the consent dialog
        # before this is ever called; this check is the independent, UI-agnostic gate.
        allowed = await self._check_permission(tool_name)
        event_type = "rbac_tool_call_allowed" if allowed else "rbac_tool_call_denied"
        await self._log(event_type, pipeline_id, project, platform, actor, {"tool": tool_name})
        if not allowed:
            raise PermissionError(f"'{tool_name}' is not an allowed tool")
        enriched = await self._enrich(arguments)
        return await self._dispatch(tool_name, enriched, project)

    async def _check_permission(self, tool_name: str) -> bool:
        result = await self._db.execute(
            select(RBACPermission.allowed).where(RBACPermission.tool_name == tool_name)
        )
        row = result.scalar_one_or_none()
        return bool(row) if row is not None else False

    async def _enrich(self, arguments: dict) -> dict:
        # Non-secret infra params come from state (passed at construction).
        # Only client_secret is fetched from Redis.
        raw = await self._redis.get(f"creds:{self._investigation_id}")
        if raw is None:
            raise RuntimeError(
                f"Credentials expired or missing for investigation {self._investigation_id}"
            )
        secret = json.loads(raw).get("client_secret")
        return {**self._infra_params, **arguments, "client_secret": secret}

    async def _dispatch(self, tool_name: str, arguments: dict, project: str) -> dict:
        fn = _TOOL_REGISTRY.get(tool_name)
        if fn is None:
            raise ValueError(f"No tool registered for '{tool_name}'")
        if asyncio.iscoroutinefunction(fn):
            # Checkpoint-enabled tools (create/update/rollback/back/forward) need Postgres
            # access (mcp_servers/adf/tools/_checkpoints.py is async-only, matching the rest
            # of this codebase's exclusively-async DB access) alongside the still-synchronous
            # Azure SDK call, which each such tool wraps in its own run_in_executor
            # internally. `db`/`project` are gateway-level context, not agent-supplied
            # arguments — never exposed in a tool's schema, so they can't collide with a
            # real parameter name.
            return await fn(db=self._db, project=project, **arguments)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(**arguments))

    async def _log(
        self,
        event_type: str,
        pipeline_id: str,
        project: str,
        platform: str,
        actor: str | None,
        detail: dict | None,
    ) -> None:
        self._db.add(AuditLog(
            investigation_id=self._investigation_id,
            pipeline_id=pipeline_id,
            project=project,
            platform=platform,
            timestamp=datetime.now(timezone.utc),
            event_type=event_type,
            actor=actor,
            detail=detail,
        ))
        await self._db.commit()
