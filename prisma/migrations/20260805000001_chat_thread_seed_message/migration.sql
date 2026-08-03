-- Replaces the failure-triggered seed text living as a fake already-sent `role=assistant`
-- ChatMessage (no LLM turn ever ran for it) with a plain column on the thread — surfaced by
-- the notification bell as an unsent suggested prompt instead, and cleared once actually sent.
ALTER TABLE "chat_threads" ADD COLUMN "seed_message" TEXT;
