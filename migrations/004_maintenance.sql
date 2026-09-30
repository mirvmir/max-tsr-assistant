-- All operational timestamps/scopes are metadata; sensitive content remains BYTEA.
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['cases','input_revisions','input_candidates','result_previews','confirmations','document_manifests','document_bundles','document_artifacts','callback_handles','inbox_events','user_identities','outbox_messages','uploaded_attachments','comparisons','jobs'] LOOP
  EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now()',t);
  EXECUTE format('ALTER TABLE %I ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()',t);
 END LOOP;
END $$;
ALTER TABLE cases ADD COLUMN IF NOT EXISTS last_activity_at timestamptz;
ALTER TABLE inbox_events ADD COLUMN IF NOT EXISTS processed_at timestamptz;
CREATE INDEX IF NOT EXISTS cases_retention ON cases(bot_scope,last_activity_at) WHERE status<>'deleted';
CREATE INDEX IF NOT EXISTS inbox_retention ON inbox_events(bot_scope,processed_at) WHERE status IN ('processed','ignored','failed');
CREATE INDEX IF NOT EXISTS artifacts_retention ON document_artifacts(bot_scope,expires_at);
CREATE TABLE IF NOT EXISTS worker_heartbeats(worker_id text NOT NULL,bot_scope text NOT NULL,release_commit text NOT NULL,heartbeat_at timestamptz NOT NULL,capacity jsonb NOT NULL DEFAULT '{}'::jsonb,PRIMARY KEY(worker_id,bot_scope));
CREATE INDEX IF NOT EXISTS worker_heartbeats_recent ON worker_heartbeats(bot_scope,heartbeat_at);
CREATE TABLE IF NOT EXISTS subscription_health(bot_scope text PRIMARY KEY,status text NOT NULL,checked_at timestamptz NOT NULL,reason_code text,secret_verified boolean NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS quota_counters(bot_scope text NOT NULL,owner_id uuid NOT NULL,kind text NOT NULL,window_start bigint NOT NULL,expires_at timestamptz NOT NULL,used integer NOT NULL,PRIMARY KEY(bot_scope,owner_id,kind,window_start));
-- Journal has no FK: it survives erasure and can be applied to an empty restore target.
CREATE TABLE IF NOT EXISTS deletion_journal(case_id uuid PRIMARY KEY,owner_id uuid NOT NULL,deletion_epoch bigint NOT NULL CHECK(deletion_epoch>0),deleted_at timestamptz NOT NULL,bot_scope text NOT NULL DEFAULT '');
INSERT INTO deletion_journal(case_id,owner_id,deletion_epoch,deleted_at,bot_scope)
 SELECT t.case_id,t.owner_id,t.deletion_epoch,t.deleted_at,c.bot_scope FROM case_tombstones t JOIN cases c ON c.id=t.case_id
 ON CONFLICT(case_id) DO NOTHING;
UPDATE inbox_events SET processed_at=updated_at WHERE processed_at IS NULL AND status IN ('processed','ignored','failed');
INSERT INTO schema_migrations(version) VALUES(4) ON CONFLICT DO NOTHING;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
ALTER TABLE active_releases ADD COLUMN IF NOT EXISTS bot_scope text NOT NULL DEFAULT 'default';
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='active_releases'::regclass AND contype='p' AND pg_get_constraintdef(oid)='PRIMARY KEY (mode)') THEN
  ALTER TABLE active_releases DROP CONSTRAINT active_releases_pkey;
  ALTER TABLE active_releases ADD PRIMARY KEY(bot_scope,mode);
 END IF;
END $$;
