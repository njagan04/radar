-- Referential integrity for the two remaining "WatchTower user id" columns that lacked a real
-- FK (chat_threads.claimed_by_user_id already had one; these two were the same kind of column,
-- just missed). Cross-schema FK, same hand-added-raw-SQL technique used everywhere else for a
-- relation into public."User" — Prisma can't model it, Postgres doesn't care.
ALTER TABLE "audit_log" ADD CONSTRAINT "audit_log_user_id_fkey"
  FOREIGN KEY ("user_id") REFERENCES public."User"("id") ON DELETE SET NULL ON UPDATE CASCADE;

ALTER TABLE "chat_analytics" ADD CONSTRAINT "chat_analytics_user_id_fkey"
  FOREIGN KEY ("user_id") REFERENCES public."User"("id") ON DELETE SET NULL ON UPDATE CASCADE;

-- Removes project_rca.error_code — confirmed dead via full-codebase grep: never written by
-- record_diagnosis_outcome (db/rca.py) or anywhere else, only ever mentioned in comments from
-- before seed-message matching moved to error_signature. Its 3-column index is dropped with it;
-- the remaining unique constraint on (pipeline_id, project, error_signature) already covers
-- every real query via the leftmost-prefix rule.
DROP INDEX "ix_project_rca_pipeline_project_error_code";
ALTER TABLE "project_rca" DROP COLUMN "error_code";

-- Removes failure_events.failure_count — confirmed dead via full-codebase grep: written once at
-- intake from WatchTower's payload, then threaded into InvestigationState["failure_count"], but
-- nothing downstream (system prompt, seed message, any tool) ever reads that state key — the
-- seed message's "seen N time(s) before" line reads project_rca.failure_count instead, a
-- different, actually-consumed recurrence counter keyed by error_signature, not this raw
-- WatchTower-supplied consecutive-failure count. WatchTower's payload still sends this field
-- (intake/schemas.py keeps accepting it so the sender doesn't break); RADAR just no longer
-- persists or threads it since nothing reads it.
ALTER TABLE "failure_events" DROP COLUMN "failure_count";
