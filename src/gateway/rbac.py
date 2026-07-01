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


def adf_infra_params(state: "dict | InvestigationState") -> dict:
    """Extract the non-secret ADF infra params from LangGraph state."""
    return {
        "tenant_id": state.get("adf_tenant_id"),
        "client_id": state.get("adf_client_id"),
        "subscription_id": state.get("adf_subscription_id"),
        "resource_group": state.get("adf_resource_group"),
        "factory_name": state.get("adf_factory_name"),
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
        role: str,
        pipeline_id: str,
        project: str,
        platform: str,
    ) -> dict:
        allowed = await self._check_permission(tool_name, role)
        event_type = "rbac_tool_call_allowed" if allowed else "rbac_tool_call_denied"
        await self._log(event_type, pipeline_id, project, platform, actor, role, {"tool": tool_name})
        if not allowed:
            raise PermissionError(f"Role '{role}' cannot call '{tool_name}'")
        enriched = await self._enrich(arguments)
        return await self._dispatch(tool_name, enriched)

    async def _check_permission(self, tool_name: str, role: str) -> bool:
        result = await self._db.execute(
            select(RBACPermission.allowed).where(
                RBACPermission.role == role,
                RBACPermission.tool_name == tool_name,
            )
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

    async def _dispatch(self, tool_name: str, arguments: dict) -> dict:
        fn = _TOOL_REGISTRY.get(tool_name)
        if fn is None:
            raise ValueError(f"No tool registered for '{tool_name}'")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(**arguments))

    async def _log(
        self,
        event_type: str,
        pipeline_id: str,
        project: str,
        platform: str,
        actor: str | None,
        role: str | None,
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
            role=role,
            detail=detail,
        ))
        await self._db.commit()
