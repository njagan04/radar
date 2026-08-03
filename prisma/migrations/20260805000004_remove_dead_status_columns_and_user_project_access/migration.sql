-- Removes columns/tables confirmed dead by a full-codebase audit (2026-08-05): no writer
-- exists for any of these, and no reader beyond echoing the value straight back out.
--
-- chat_threads.status: set to "open" at creation, never transitioned, never read except
-- being echoed back verbatim in the thread API response (which the frontend's Thread type
-- doesn't even have a field for).
--
-- failure_events.status/diagnosis_result: vestigial since the old structured-diagnosis
-- pipeline was retired 2026-07-28 (run_diagnosis(), its only writer, was deleted then) —
-- nothing has transitioned status past "pending_diagnosis" or written diagnosis_result since.
--
-- user_project_access: superseded 2026-07-29 by reading WatchTower's own
-- public."UserProjectAssignment" directly (see chat/access.py, chat/thread_setup.py) — this
-- table has had zero real readers/writers since that switch, only comments referencing it.
ALTER TABLE "chat_threads" DROP COLUMN "status";
ALTER TABLE "failure_events" DROP COLUMN "status";
ALTER TABLE "failure_events" DROP COLUMN "diagnosis_result";
DROP TABLE "user_project_access";
