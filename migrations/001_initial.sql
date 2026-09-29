-- Sensitive DTOs are encrypted BYTEA. Only operational identifiers are indexed.
CREATE TABLE IF NOT EXISTS schema_migrations(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS cases (
 id uuid PRIMARY KEY, owner_id uuid NOT NULL, case_id uuid, revision bigint, deletion_epoch bigint NOT NULL DEFAULT 0,
 bot_scope text NOT NULL DEFAULT '', status text NOT NULL DEFAULT 'active', dedupe_key text,
 parent_id uuid, manifest_id uuid, preview_id uuid, payload_ref uuid, kind text, opaque_key text,
 expires_at timestamptz, active boolean NOT NULL DEFAULT true,
 key_id text NOT NULL, nonce bytea NOT NULL, ciphertext bytea NOT NULL, tag bytea NOT NULL,
 UNIQUE(owner_id,id)
);
CREATE INDEX IF NOT EXISTS cases_owner_live ON cases(owner_id,status);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['input_revisions','input_candidates','result_previews','confirmations','document_manifests','document_bundles','document_artifacts','callback_handles','inbox_events','user_identities','outbox_messages'] LOOP
  EXECUTE format('CREATE TABLE IF NOT EXISTS %I (LIKE cases INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES)',t);
  EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I(owner_id,case_id)',t || '_owner_case',t);
 END LOOP;
END $$;
-- FK enforces scope in addition to repository authorization. Null case_id is for global infrastructure records.
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['input_revisions','input_candidates','result_previews','confirmations','document_manifests','document_bundles','document_artifacts','callback_handles','outbox_messages'] LOOP
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE connamespace=current_schema()::regnamespace AND conname=t || '_case_owner_fk') THEN
   EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (owner_id,case_id) REFERENCES cases(owner_id,id)',t,t || '_case_owner_fk');
  END IF;
 END LOOP;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS identity_lookup_unique ON user_identities(bot_scope,opaque_key);
CREATE UNIQUE INDEX IF NOT EXISTS inbox_dedupe_unique ON inbox_events(bot_scope,owner_id,dedupe_key);
CREATE UNIQUE INDEX IF NOT EXISTS handle_opaque_unique ON callback_handles(opaque_key);
CREATE UNIQUE INDEX IF NOT EXISTS confirmation_preview_unique ON confirmations(owner_id,case_id,preview_id);
CREATE UNIQUE INDEX IF NOT EXISTS manifest_confirmation_unique ON document_manifests(owner_id,case_id,parent_id);
CREATE UNIQUE INDEX IF NOT EXISTS bundle_manifest_unique ON document_bundles(owner_id,case_id,manifest_id);
CREATE UNIQUE INDEX IF NOT EXISTS artifact_manifest_kind_unique ON document_artifacts(owner_id,case_id,manifest_id,kind);
CREATE UNIQUE INDEX IF NOT EXISTS outbox_dedupe_unique ON outbox_messages(bot_scope,owner_id,COALESCE(case_id,'00000000-0000-0000-0000-000000000000'::uuid),dedupe_key);
CREATE TABLE IF NOT EXISTS jobs (
 id uuid PRIMARY KEY, owner_id uuid NOT NULL, case_id uuid, revision bigint, deletion_epoch bigint NOT NULL,
 bot_scope text NOT NULL, kind text NOT NULL, payload_ref uuid NOT NULL, dedupe_key text NOT NULL,
 inbox_id uuid REFERENCES inbox_events(id), manifest_id uuid REFERENCES document_manifests(id), outbox_id uuid REFERENCES outbox_messages(id),
 status text NOT NULL DEFAULT 'queued', fence_token bigint NOT NULL DEFAULT 0, lease_owner text, lease_until timestamptz,
 attempt integer NOT NULL DEFAULT 0,next_attempt_at timestamptz NOT NULL,trace_id uuid NOT NULL,
 key_id text NOT NULL, nonce bytea NOT NULL,ciphertext bytea NOT NULL,tag bytea NOT NULL,
 FOREIGN KEY(owner_id,case_id) REFERENCES cases(owner_id,id),
 CHECK((kind='process_inbox' AND inbox_id=payload_ref) OR (kind='render_artifact' AND manifest_id=payload_ref) OR (kind='deliver_outbox' AND outbox_id=payload_ref))
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_scope_dedupe ON jobs(bot_scope,owner_id,COALESCE(case_id,'00000000-0000-0000-0000-000000000000'::uuid),kind,dedupe_key);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(kind,next_attempt_at) WHERE status IN ('queued','retry_wait');
CREATE INDEX IF NOT EXISTS jobs_lease ON jobs(lease_until) WHERE status='running';
CREATE TABLE IF NOT EXISTS send_attempts(id uuid PRIMARY KEY,outbox_id uuid NOT NULL REFERENCES outbox_messages(id),job_id uuid NOT NULL REFERENCES jobs(id),fence_token bigint NOT NULL,status text NOT NULL,key_id text NOT NULL,nonce bytea NOT NULL,ciphertext bytea NOT NULL,tag bytea NOT NULL);
CREATE TABLE IF NOT EXISTS case_tombstones(case_id uuid PRIMARY KEY REFERENCES cases(id),owner_id uuid NOT NULL,deletion_epoch bigint NOT NULL,deleted_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS cleanup_requests(case_id uuid PRIMARY KEY REFERENCES cases(id),deletion_epoch bigint NOT NULL,status text NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS uploaded_attachments(id uuid PRIMARY KEY,owner_id uuid NOT NULL,artifact_id uuid NOT NULL REFERENCES document_artifacts(id),manifest_id uuid NOT NULL REFERENCES document_manifests(id),key_id text NOT NULL,nonce bytea NOT NULL,ciphertext bytea NOT NULL,tag bytea NOT NULL);
-- Public release content remains immutable; lifecycle/activation is separate.
CREATE TABLE IF NOT EXISTS data_releases(id text NOT NULL,version text NOT NULL,content jsonb NOT NULL,content_hash text NOT NULL,PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS data_lifecycle(id text NOT NULL,version text NOT NULL,revoked boolean NOT NULL DEFAULT false,review_due_at timestamptz,PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS active_releases(mode text PRIMARY KEY,release_id text NOT NULL,release_version text NOT NULL,FOREIGN KEY(release_id,release_version) REFERENCES data_releases(id,version));
CREATE TABLE IF NOT EXISTS source_audit(id uuid PRIMARY KEY,subject_id text NOT NULL,actor_key text NOT NULL,occurred_at timestamptz NOT NULL,result_code text NOT NULL);
INSERT INTO schema_migrations(version) VALUES(1) ON CONFLICT DO NOTHING;
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE connamespace=current_schema()::regnamespace AND conname='artifact_manifest_owner_fk') THEN
  ALTER TABLE document_artifacts ADD CONSTRAINT artifact_manifest_owner_fk FOREIGN KEY(owner_id,manifest_id) REFERENCES document_manifests(owner_id,id);
  ALTER TABLE document_bundles ADD CONSTRAINT bundle_manifest_owner_fk FOREIGN KEY(owner_id,manifest_id) REFERENCES document_manifests(owner_id,id);
  ALTER TABLE document_manifests ADD CONSTRAINT manifest_confirmation_owner_fk FOREIGN KEY(owner_id,parent_id) REFERENCES confirmations(owner_id,id);
 END IF;
END $$;
CREATE TABLE IF NOT EXISTS comparisons (LIKE cases INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING INDEXES);
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE connamespace=current_schema()::regnamespace AND conname='comparisons_case_owner_fk') THEN
  ALTER TABLE comparisons ADD CONSTRAINT comparisons_case_owner_fk FOREIGN KEY(owner_id,case_id) REFERENCES cases(owner_id,id);
 END IF;
END $$;
