-- CreateTable
CREATE TABLE "project_metadata" (
    "project" TEXT NOT NULL,
    "platform" TEXT,

    CONSTRAINT "project_metadata_pkey" PRIMARY KEY ("project")
);

-- CreateTable
CREATE TABLE "credentials" (
    "id" BIGSERIAL NOT NULL,
    "project" TEXT NOT NULL,
    "resource_group" TEXT NOT NULL,
    "factory_name" TEXT NOT NULL,
    "tenant_id" TEXT NOT NULL,
    "client_id" TEXT NOT NULL,
    "subscription_id" TEXT NOT NULL,
    "key_vault_uri" TEXT,

    CONSTRAINT "credentials_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "user_project_access" (
    "user_email" TEXT NOT NULL,
    "project" TEXT NOT NULL,
    "notify_on_failure" BOOLEAN NOT NULL DEFAULT true,
    "user_id" TEXT,

    CONSTRAINT "user_project_access_pkey" PRIMARY KEY ("user_email","project")
);

-- CreateTable
CREATE TABLE "rbac_permissions" (
    "tool_name" TEXT NOT NULL,
    "allowed" BOOLEAN NOT NULL,
    "requires_consent" BOOLEAN NOT NULL DEFAULT true,

    CONSTRAINT "rbac_permissions_pkey" PRIMARY KEY ("tool_name")
);

-- CreateTable
CREATE TABLE "project_rca" (
    "id" BIGSERIAL NOT NULL,
    "pipeline_id" TEXT NOT NULL,
    "project" TEXT NOT NULL,
    "error_signature" TEXT NOT NULL,
    "error_code" TEXT,
    "error_category" TEXT NOT NULL,
    "impact" TEXT,
    "root_cause" TEXT,
    "fix_applied" TEXT,
    "preventive_steps" TEXT,
    "invocation_count" INTEGER NOT NULL DEFAULT 1,
    "last_failure_timestamp" TIMESTAMPTZ,
    "denial_history" JSONB NOT NULL DEFAULT '[]',
    "last_rerun_attempt" JSONB,
    "updated_at" TIMESTAMPTZ,

    CONSTRAINT "project_rca_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "audit_log" (
    "audit_id" BIGSERIAL NOT NULL,
    "investigation_id" TEXT,
    "pipeline_id" TEXT NOT NULL,
    "project" TEXT NOT NULL,
    "platform" TEXT NOT NULL,
    "timestamp" TIMESTAMPTZ NOT NULL,
    "event_type" TEXT NOT NULL,
    "actor" TEXT,
    "detail" JSONB,

    CONSTRAINT "audit_log_pkey" PRIMARY KEY ("audit_id")
);

-- CreateTable
CREATE TABLE "failure_events" (
    "investigation_id" TEXT NOT NULL,
    "project" TEXT NOT NULL,
    "platform" TEXT NOT NULL,
    "pipeline_name" TEXT NOT NULL,
    "factory_name" TEXT,
    "run_status" TEXT NOT NULL,
    "start_time" TIMESTAMPTZ NOT NULL,
    "end_time" TIMESTAMPTZ,
    "last_error" TEXT,
    "error_detail" JSONB,
    "failure_count" INTEGER NOT NULL DEFAULT 0,
    "trigger_type" TEXT,
    "status" TEXT NOT NULL DEFAULT 'pending_diagnosis',
    "diagnosis_result" JSONB,
    "created_at" TIMESTAMPTZ NOT NULL,
    "updated_at" TIMESTAMPTZ,

    CONSTRAINT "failure_events_pkey" PRIMARY KEY ("investigation_id")
);

-- CreateTable
CREATE TABLE "chat_threads" (
    "thread_id" BIGSERIAL NOT NULL,
    "project" TEXT NOT NULL,
    "investigation_id" TEXT,
    "claimed_by_user_email" TEXT,
    "context_summary" TEXT,
    "summarized_through_timestamp" TIMESTAMPTZ,
    "status" TEXT,
    "pending_tool_approval" JSONB,
    "created_at" TIMESTAMPTZ NOT NULL,
    "updated_at" TIMESTAMPTZ,

    CONSTRAINT "chat_threads_pkey" PRIMARY KEY ("thread_id")
);

-- CreateTable
CREATE TABLE "chat_messages" (
    "id" BIGSERIAL NOT NULL,
    "thread_id" BIGINT NOT NULL,
    "role" TEXT NOT NULL,
    "content" TEXT NOT NULL,
    "tool_calls" JSONB,
    "created_at" TIMESTAMPTZ NOT NULL,

    CONSTRAINT "chat_messages_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "resource_snapshot_blobs" (
    "hash" TEXT NOT NULL,
    "definition" JSONB NOT NULL,

    CONSTRAINT "resource_snapshot_blobs_pkey" PRIMARY KEY ("hash")
);

-- CreateTable
CREATE TABLE "resource_snapshots" (
    "id" BIGSERIAL NOT NULL,
    "project" TEXT NOT NULL,
    "kind" TEXT NOT NULL,
    "resource_name" TEXT NOT NULL,
    "sequence" INTEGER NOT NULL,
    "state_name" TEXT NOT NULL,
    "action" TEXT NOT NULL,
    "reason" TEXT NOT NULL,
    "change_summary" TEXT,
    "blob_hash" TEXT,
    "created_at" TIMESTAMPTZ NOT NULL,

    CONSTRAINT "resource_snapshots_pkey" PRIMARY KEY ("id")
);

-- CreateTable
CREATE TABLE "resource_snapshot_cursor" (
    "project" TEXT NOT NULL,
    "kind" TEXT NOT NULL,
    "resource_name" TEXT NOT NULL,
    "current_sequence" INTEGER NOT NULL,

    CONSTRAINT "resource_snapshot_cursor_pkey" PRIMARY KEY ("project","kind","resource_name")
);

-- CreateIndex
CREATE UNIQUE INDEX "credentials_project_factory_name_key" ON "credentials"("project", "factory_name");

-- CreateIndex
CREATE INDEX "ix_project_rca_pipeline_project_error_code" ON "project_rca"("pipeline_id", "project", "error_code");

-- CreateIndex
CREATE UNIQUE INDEX "project_rca_pipeline_id_project_error_signature_key" ON "project_rca"("pipeline_id", "project", "error_signature");

-- CreateIndex
CREATE INDEX "audit_log_investigation_id_idx" ON "audit_log"("investigation_id");

-- CreateIndex
CREATE INDEX "ix_failure_events_project_pipeline_created" ON "failure_events"("project", "pipeline_name", "created_at");

-- CreateIndex
CREATE UNIQUE INDEX "chat_threads_investigation_id_key" ON "chat_threads"("investigation_id");

-- CreateIndex
CREATE INDEX "chat_threads_project_idx" ON "chat_threads"("project");

-- CreateIndex
CREATE INDEX "chat_threads_claimed_by_user_email_idx" ON "chat_threads"("claimed_by_user_email");

-- CreateIndex
CREATE INDEX "ix_chat_messages_thread_created" ON "chat_messages"("thread_id", "created_at");

-- CreateIndex
CREATE UNIQUE INDEX "resource_snapshots_project_kind_resource_name_sequence_key" ON "resource_snapshots"("project", "kind", "resource_name", "sequence");

-- AddForeignKey
ALTER TABLE "credentials" ADD CONSTRAINT "credentials_project_fkey" FOREIGN KEY ("project") REFERENCES "project_metadata"("project") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "user_project_access" ADD CONSTRAINT "user_project_access_project_fkey" FOREIGN KEY ("project") REFERENCES "project_metadata"("project") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "project_rca" ADD CONSTRAINT "project_rca_project_fkey" FOREIGN KEY ("project") REFERENCES "project_metadata"("project") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "audit_log" ADD CONSTRAINT "audit_log_investigation_id_fkey" FOREIGN KEY ("investigation_id") REFERENCES "failure_events"("investigation_id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "failure_events" ADD CONSTRAINT "failure_events_project_fkey" FOREIGN KEY ("project") REFERENCES "project_metadata"("project") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "failure_events" ADD CONSTRAINT "failure_events_project_factory_name_fkey" FOREIGN KEY ("project", "factory_name") REFERENCES "credentials"("project", "factory_name") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "chat_threads" ADD CONSTRAINT "chat_threads_project_fkey" FOREIGN KEY ("project") REFERENCES "project_metadata"("project") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "chat_threads" ADD CONSTRAINT "chat_threads_investigation_id_fkey" FOREIGN KEY ("investigation_id") REFERENCES "failure_events"("investigation_id") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "chat_messages" ADD CONSTRAINT "chat_messages_thread_id_fkey" FOREIGN KEY ("thread_id") REFERENCES "chat_threads"("thread_id") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "resource_snapshots" ADD CONSTRAINT "resource_snapshots_project_fkey" FOREIGN KEY ("project") REFERENCES "project_metadata"("project") ON DELETE RESTRICT ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "resource_snapshots" ADD CONSTRAINT "resource_snapshots_blob_hash_fkey" FOREIGN KEY ("blob_hash") REFERENCES "resource_snapshot_blobs"("hash") ON DELETE SET NULL ON UPDATE CASCADE;

-- AddForeignKey
ALTER TABLE "resource_snapshot_cursor" ADD CONSTRAINT "resource_snapshot_cursor_project_kind_resource_name_curren_fkey" FOREIGN KEY ("project", "kind", "resource_name", "current_sequence") REFERENCES "resource_snapshots"("project", "kind", "resource_name", "sequence") ON DELETE RESTRICT ON UPDATE CASCADE;

-- CheckConstraints (hand-added — Prisma's schema language has no CHECK support;
-- these mirror the backend's original Alembic-managed constraints exactly)
ALTER TABLE "chat_messages" ADD CONSTRAINT "ck_chat_messages_role" CHECK ("role" IN ('user', 'assistant'));

ALTER TABLE "resource_snapshots" ADD CONSTRAINT "ck_resource_snapshots_kind" CHECK ("kind" IN ('pipeline', 'dataset', 'linked_service', 'data_flow', 'trigger', 'global_parameter'));

ALTER TABLE "resource_snapshots" ADD CONSTRAINT "ck_resource_snapshots_action" CHECK ("action" IN ('exists', 'create'));

ALTER TABLE "resource_snapshots" ADD CONSTRAINT "ck_resource_snapshots_blob_hash_nullability" CHECK (("action" = 'create' AND "blob_hash" IS NULL) OR ("action" = 'exists' AND "blob_hash" IS NOT NULL));
