-- Start creates input+case atomically, so the initial input may precede its case.
ALTER TABLE input_revisions ALTER CONSTRAINT input_revisions_case_owner_fk DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE data_lifecycle ADD COLUMN IF NOT EXISTS reason_code text;
INSERT INTO schema_migrations(version) VALUES(3) ON CONFLICT DO NOTHING;
