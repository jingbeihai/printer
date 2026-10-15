-- Run against a stopped server after a verified PostgreSQL backup.
BEGIN;
ALTER TABLE school_presets ADD COLUMN overrides_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN class_copies_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN cover_enabled INTEGER NOT NULL DEFAULT 0;
COMMIT;
