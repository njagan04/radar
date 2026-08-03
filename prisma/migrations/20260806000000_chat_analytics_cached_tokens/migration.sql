-- Tracks how many of a turn's input_tokens Azure served from its own automatic prompt
-- cache (confirmed live on this deployment) — a subset of input_tokens, not a separate pool,
-- so chat/service.py's _estimate_cost can price it at the discounted cached-input rate
-- instead of treating every input token as full price.
ALTER TABLE "chat_analytics" ADD COLUMN "cached_tokens" INTEGER NOT NULL DEFAULT 0;
