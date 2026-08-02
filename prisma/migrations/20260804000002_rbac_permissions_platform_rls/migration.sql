-- Platform-scoped Postgres RLS on rbac_permissions — DB-level backstop alongside the existing
-- app-level ".where(RBACPermission.platform == state['platform'])" filter (tool_search_tool.py)
-- so a future query that forgets that filter can't silently see/return another platform's
-- permission rows. Session-scoped via app.current_platform (SET LOCAL, per-transaction) —
-- same technique this app already uses conceptually for per-user RLS (see chat/access.py's
-- docstring on the deferred UserProjectAccess RLS pass).
--
-- IMPORTANT — this policy has NO effect while the app connects as a Postgres superuser (or the
-- table owner, unless FORCE is set — which this migration does set, but FORCE still does not
-- override superuser bypass; nothing can). Superusers bypass row security unconditionally, by
-- design, with no override. Verified live: this database's app connection role is `postgres`,
-- which is both. For this policy to actually enforce anything, the app must connect as a
-- separate, non-superuser role instead (ordinary GRANTed privileges on rbac_permissions, no
-- BYPASSRLS). That role change is a deployment/ops step, not something this migration can do.

ALTER TABLE "rbac_permissions" ENABLE ROW LEVEL SECURITY;
ALTER TABLE "rbac_permissions" FORCE ROW LEVEL SECURITY;

CREATE POLICY "rbac_permissions_platform_isolation" ON "rbac_permissions"
    USING ("platform" = current_setting('app.current_platform', true));
