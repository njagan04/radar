-- Course-correction on 20260805000001: a pending seed message shouldn't require creating a
-- real ChatThread before anyone has actually opened/engaged with the notification (we have no
-- way to know in advance whether a given notification will ever be opened). Moves the pending
-- text onto FailureEvent instead, which already exists per-failure regardless of chat activity.
-- resolved_thread_id stays NULL until a human actually sends something, at which point a real
-- ChatThread gets created (chat/service.py's create_ad_hoc_thread) and stamps this column.
ALTER TABLE "chat_threads" DROP COLUMN "seed_message";
ALTER TABLE "failure_events" ADD COLUMN "seed_message" TEXT;
ALTER TABLE "failure_events" ADD COLUMN "resolved_thread_id" UUID REFERENCES "chat_threads"("thread_id");
