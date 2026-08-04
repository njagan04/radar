-- Drops impact (never a distinct enough concept from fix_applied to be worth its own column
-- in practice) and folds preventive_steps into fix_applied (same field now covers "what fixed
-- it" and "how to prevent it recurring" — callers never needed the split, and merging saves a
-- redundant column on every future lookup/seed message).
UPDATE "project_rca"
SET "fix_applied" = CASE
    WHEN "fix_applied" IS NULL THEN "preventive_steps"
    WHEN "preventive_steps" IS NULL THEN "fix_applied"
    ELSE "fix_applied" || ' | ' || "preventive_steps"
END
WHERE "preventive_steps" IS NOT NULL;

ALTER TABLE "project_rca" DROP COLUMN "impact";
ALTER TABLE "project_rca" DROP COLUMN "preventive_steps";
