-- audit_log improvements:
--   1. pipeline_id -> pipeline_name: it was always the pipeline's own name string (never a
--      surrogate key), matching failure_events.pipeline_name — a plain rename, no data change.
--   2. actor (email, human-readable only, never used for authorization) -> user_id (uuid,
--      WatchTower's public."User".id — the same real identity chat_threads.claimed_by_user_id
--      already uses). Existing rows hold either a real email or a system placeholder
--      ("investigator"/"intake") — backfilled via a join on email where possible; rows that
--      were a placeholder (no matching User) end up NULL, which is correct: no real user
--      triggered those.
--   3. New thread_id (uuid, FK to chat_threads) — previously nothing on this table linked a
--      tool-call audit row to the chat thread it happened in; investigation_id alone doesn't
--      cover it (NULL for every ad-hoc, non-failure-triggered thread, which is most chat usage
--      today). Nullable and NOT backfilled for existing rows — there's no reliable historical
--      signal to reconstruct it from (investigation_id is NULL for exactly the rows that need
--      it); only rows written going forward get it populated.

ALTER TABLE "audit_log" RENAME COLUMN "pipeline_id" TO "pipeline_name";

ALTER TABLE "audit_log" ADD COLUMN "user_id" UUID;
UPDATE "audit_log" al
SET "user_id" = u.id
FROM public."User" u
WHERE u.email = al."actor";
ALTER TABLE "audit_log" DROP COLUMN "actor";

ALTER TABLE "audit_log" ADD COLUMN "thread_id" UUID;
ALTER TABLE "audit_log" ADD CONSTRAINT "audit_log_thread_id_fkey" FOREIGN KEY ("thread_id") REFERENCES "chat_threads"("thread_id") ON DELETE SET NULL ON UPDATE CASCADE;
CREATE INDEX "ix_audit_log_thread_id" ON "audit_log"("thread_id");
