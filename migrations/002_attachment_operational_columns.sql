ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS case_id uuid;
ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS revision bigint;
ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS deletion_epoch bigint NOT NULL DEFAULT 0;
ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS bot_scope text NOT NULL DEFAULT '';
ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';
ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS dedupe_key text;
ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS parent_id uuid;
ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS preview_id uuid;
ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS payload_ref uuid;
ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS kind text;
ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS opaque_key text;
ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS expires_at timestamptz;
ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS active boolean NOT NULL DEFAULT true;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE connamespace=current_schema()::regnamespace AND conname='uploaded_artifact_owner_fk') THEN
  ALTER TABLE uploaded_attachments ADD CONSTRAINT uploaded_artifact_owner_fk FOREIGN KEY(owner_id,artifact_id) REFERENCES document_artifacts(owner_id,id);
 END IF;
END $$;
INSERT INTO schema_migrations(version) VALUES(2) ON CONFLICT DO NOTHING;
