-- Backfill: a handful of existing claimed threads have claimed_by_user_email set but
-- claimed_by_user_id still null (claimed before the id column existed, never backfilled).
-- Must run before the email column is dropped below, or those threads would silently become
-- unclaimed once radar's own code stops reading email at all.
UPDATE "chat_threads" AS ct
SET "claimed_by_user_id" = u."id"
FROM public."User" u
WHERE u."email" = ct."claimed_by_user_email"
  AND ct."claimed_by_user_id" IS NULL
  AND ct."claimed_by_user_email" IS NOT NULL;

-- AlterTable: claimed_by_user_email retired — every authorization check (claim_thread,
-- _ensure_claimed_by, prepare_tool_approval_resolution in radar/src/chat/service.py) now keys
-- on claimed_by_user_id, carried directly in the verified X-Radar-Assertion JWT.
DROP INDEX "chat_threads_claimed_by_user_email_idx";
ALTER TABLE "chat_threads" DROP COLUMN "claimed_by_user_email";

CREATE INDEX "chat_threads_claimed_by_user_id_idx" ON "chat_threads"("claimed_by_user_id");
