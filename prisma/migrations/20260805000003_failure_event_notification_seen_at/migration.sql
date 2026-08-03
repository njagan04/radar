-- Tracks "has a human looked at this notification" separately from resolved_thread_id (which
-- only means a real chat got created from it) — drives the new-vs-already-opened distinction
-- in the notification list.
ALTER TABLE "failure_events" ADD COLUMN "seen_at" TIMESTAMPTZ;
