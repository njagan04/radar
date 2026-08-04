-- Removes denial_history/last_rerun_attempt — confirmed dead via a full-codebase grep: read in
-- two places (llm/tools.py's check_known_fix, chat/seed_message.py's build_seed_message) but
-- written in ZERO places, anywhere. The rerun-execution module that used to maintain this
-- bookkeeping was explicitly retired 2026-07-28 (see mcp_servers/adf/tool_search_tool.py's own
-- module docstring: "no RCA denial_history bookkeeping specific to reruns"), leaving these two
-- columns permanently empty/null for every row since.
--
-- Renames invocation_count -> failure_count — it always tracked "how many times has this
-- specific error recurred"; the new name says that directly instead of requiring the reader to
-- infer it.
ALTER TABLE "project_rca" DROP COLUMN "denial_history";
ALTER TABLE "project_rca" DROP COLUMN "last_rerun_attempt";
ALTER TABLE "project_rca" RENAME COLUMN "invocation_count" TO "failure_count";
