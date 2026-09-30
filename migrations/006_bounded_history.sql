ALTER TABLE comparisons ADD COLUMN IF NOT EXISTS comparison_input_revision_id uuid;
ALTER TABLE comparisons ADD COLUMN IF NOT EXISTS comparison_snapshot_id uuid;
CREATE INDEX IF NOT EXISTS comparisons_guard_snapshot ON comparisons
 (owner_id,case_id,revision,deletion_epoch,comparison_input_revision_id,comparison_snapshot_id,created_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS cases_owner_history ON cases(owner_id,last_activity_at DESC,id DESC)
 WHERE status NOT IN ('deleted','deleting') AND octet_length(ciphertext)>0;
CREATE INDEX IF NOT EXISTS artifacts_case_history ON document_artifacts(case_id,created_at DESC,id DESC)
 WHERE status='published' AND octet_length(ciphertext)>0;
CREATE INDEX IF NOT EXISTS bundles_case_history ON document_bundles(case_id,created_at DESC,id DESC)
 WHERE status<>'deleted' AND octet_length(ciphertext)>0;
CREATE INDEX IF NOT EXISTS outbox_case_unknown ON outbox_messages(case_id,created_at DESC,id DESC)
 WHERE status='delivery_unknown' AND octet_length(ciphertext)>0;
INSERT INTO schema_migrations(version) VALUES(6) ON CONFLICT DO NOTHING;
