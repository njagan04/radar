-- AlterTable
ALTER TABLE "chat_threads" ADD COLUMN     "claimed_by_user_id" UUID,
ADD COLUMN     "is_deleted" BOOLEAN NOT NULL DEFAULT false;

-- AlterTable
ALTER TABLE "rbac_permissions" ADD COLUMN     "platform" TEXT NOT NULL DEFAULT 'adf';

-- Cross-schema FK (hand-added — Prisma can't model a relation into another schema file,
-- same technique already used for public."UserProjectAssignment")
ALTER TABLE "chat_threads" ADD CONSTRAINT "chat_threads_claimed_by_user_id_fkey"
  FOREIGN KEY ("claimed_by_user_id") REFERENCES public."User"("id") ON DELETE SET NULL ON UPDATE CASCADE;
