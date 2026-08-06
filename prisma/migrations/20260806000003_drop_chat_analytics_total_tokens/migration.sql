-- total_tokens always equaled input_tokens + output_tokens, computed and persisted redundantly
-- at every write site with no constraint enforcing the invariant. Never read back anywhere
-- (confirmed via a full-codebase grep: only ever written, never selected) — derived in Python
-- instead (ChatAnalytics.total_tokens is now a @property on the model).
ALTER TABLE "chat_analytics" DROP COLUMN "total_tokens";
