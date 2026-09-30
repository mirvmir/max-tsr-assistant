-- Only opaque internal references, stable command types, and keyed digests.
CREATE TABLE IF NOT EXISTS private_index_keys (
 id integer PRIMARY KEY CHECK(id=1), key_id text NOT NULL,
 nonce bytea NOT NULL,ciphertext bytea NOT NULL,tag bytea NOT NULL
);
ALTER TABLE user_identities ADD COLUMN IF NOT EXISTS delivery_target_id uuid;
ALTER TABLE input_candidates ADD COLUMN IF NOT EXISTS dialog_revision bigint;
ALTER TABLE callback_handles ADD COLUMN IF NOT EXISTS dialog_revision bigint;
ALTER TABLE callback_handles ADD COLUMN IF NOT EXISTS action_command_type text;
ALTER TABLE outbox_messages ADD COLUMN IF NOT EXISTS dialog_revision bigint;
ALTER TABLE outbox_messages ADD COLUMN IF NOT EXISTS payload_kind text;
ALTER TABLE outbox_messages ADD COLUMN IF NOT EXISTS delivery_target_id uuid;
ALTER TABLE outbox_messages ADD COLUMN IF NOT EXISTS receipt_digest text;
ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS manifest_hash text;
ALTER TABLE uploaded_attachments ADD COLUMN IF NOT EXISTS observed_at timestamptz;
CREATE INDEX IF NOT EXISTS attachments_exact_latest ON uploaded_attachments(owner_id,artifact_id,manifest_hash,observed_at DESC,id DESC) WHERE octet_length(ciphertext)>0;
CREATE INDEX IF NOT EXISTS identities_owner_target ON user_identities(owner_id,delivery_target_id,id) WHERE octet_length(ciphertext)>0;
CREATE INDEX IF NOT EXISTS candidates_guard_latest ON input_candidates(owner_id,case_id,revision,deletion_epoch,created_at DESC,id DESC) WHERE active AND octet_length(ciphertext)>0;
CREATE INDEX IF NOT EXISTS candidates_dialog_latest ON input_candidates(owner_id,case_id,revision,deletion_epoch,dialog_revision,created_at DESC,id DESC) WHERE active AND octet_length(ciphertext)>0;
CREATE INDEX IF NOT EXISTS outbox_dialog_latest ON outbox_messages(owner_id,case_id,dialog_revision,created_at DESC,id DESC) WHERE payload_kind='view' AND octet_length(ciphertext)>0;
CREATE INDEX IF NOT EXISTS outbox_owner_case_latest ON outbox_messages(owner_id,case_id,created_at DESC,id DESC) WHERE octet_length(ciphertext)>0;
CREATE INDEX IF NOT EXISTS outbox_receipt_bound ON outbox_messages(owner_id,delivery_target_id,receipt_digest,id) WHERE status='confirmed' AND octet_length(ciphertext)>0;
CREATE INDEX IF NOT EXISTS handles_global_role_active ON callback_handles(owner_id) WHERE case_id IS NULL AND active AND action_command_type='start_case';
CREATE INDEX IF NOT EXISTS jobs_case_invalidation ON jobs(owner_id,case_id,revision,id) WHERE status IN ('queued','retry_wait','running');
INSERT INTO schema_migrations(version) VALUES(7) ON CONFLICT DO NOTHING;
