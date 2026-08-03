-- chat_analytics: real per-turn token usage/cost (summed from the OpenAI Agents SDK's own
-- Usage objects, not the char-based estimate the UI's live token_count display uses) — nothing
-- reads this yet, it exists so a future cost/usage dashboard doesn't need a backfill.
-- message_feedback: backend for the thumbs up/down buttons MessageActions already renders in
-- the frontend with purely-local state.

CREATE TABLE "chat_analytics" (
    "id" BIGSERIAL PRIMARY KEY,
    "thread_id" UUID NOT NULL REFERENCES "chat_threads"("thread_id"),
    "user_id" UUID,
    "project" TEXT NOT NULL,
    "platform" TEXT NOT NULL,
    "model" TEXT NOT NULL,
    "input_tokens" INTEGER NOT NULL DEFAULT 0,
    "output_tokens" INTEGER NOT NULL DEFAULT 0,
    "total_tokens" INTEGER NOT NULL DEFAULT 0,
    "estimated_cost" DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    "created_at" TIMESTAMPTZ NOT NULL
);

CREATE INDEX "chat_analytics_thread_id_idx" ON "chat_analytics"("thread_id");
CREATE INDEX "chat_analytics_user_id_idx" ON "chat_analytics"("user_id");

CREATE TABLE "message_feedback" (
    "id" BIGSERIAL PRIMARY KEY,
    "message_id" BIGINT NOT NULL REFERENCES "chat_messages"("id"),
    "user_id" UUID NOT NULL,
    "rating" TEXT NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL,
    "updated_at" TIMESTAMPTZ,
    CONSTRAINT "ck_message_feedback_rating" CHECK ("rating" IN ('up', 'down')),
    CONSTRAINT "uq_message_feedback_message_user" UNIQUE ("message_id", "user_id")
);
