-- chat_threads.thread_id / chat_messages.thread_id: BigInt -> UUID. There's real data (live
-- threads/messages), so this is a mapped rewrite (new column, backfilled via the old<->new
-- mapping, old column dropped, new one renamed into place) rather than a plain ALTER COLUMN
-- TYPE, which has no numeric->uuid conversion path.

-- 1. New UUID columns, distinct random value per existing row.
ALTER TABLE "chat_threads" ADD COLUMN "thread_id_new" UUID NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE "chat_messages" ADD COLUMN "thread_id_new" UUID;

-- 2. Backfill chat_messages' new FK column via the old id -> new uuid mapping just created.
UPDATE "chat_messages" cm
SET "thread_id_new" = ct."thread_id_new"
FROM "chat_threads" ct
WHERE ct."thread_id" = cm."thread_id";

ALTER TABLE "chat_messages" ALTER COLUMN "thread_id_new" SET NOT NULL;

-- 3. Drop everything referencing the old bigint column before dropping it.
ALTER TABLE "chat_messages" DROP CONSTRAINT "chat_messages_thread_id_fkey";
DROP INDEX "ix_chat_messages_thread_created";
ALTER TABLE "chat_threads" DROP CONSTRAINT "chat_threads_pkey";

-- 4. Swap old -> new.
ALTER TABLE "chat_messages" DROP COLUMN "thread_id";
ALTER TABLE "chat_messages" RENAME COLUMN "thread_id_new" TO "thread_id";
ALTER TABLE "chat_threads" DROP COLUMN "thread_id";
ALTER TABLE "chat_threads" RENAME COLUMN "thread_id_new" TO "thread_id";

-- 5. Recreate the PK/FK/index that used to cover the old column.
ALTER TABLE "chat_threads" ADD CONSTRAINT "chat_threads_pkey" PRIMARY KEY ("thread_id");
ALTER TABLE "chat_messages" ADD CONSTRAINT "chat_messages_thread_id_fkey" FOREIGN KEY ("thread_id") REFERENCES "chat_threads"("thread_id") ON DELETE RESTRICT ON UPDATE CASCADE;
CREATE INDEX "ix_chat_messages_thread_created" ON "chat_messages"("thread_id", "created_at");
