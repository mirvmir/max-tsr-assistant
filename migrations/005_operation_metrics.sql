-- Bounded per-scope aggregates only. Never store owner, job, trace, or payload.
CREATE TABLE IF NOT EXISTS operation_metrics (
 bot_scope text NOT NULL,
 name text NOT NULL CHECK(name IN ('queue.process_inbox','queue.render_artifact','queue.deliver_outbox','compute.process_inbox','compute.render_artifact','compute.deliver_outbox')),
 count bigint NOT NULL CHECK(count>=0),
 total_seconds double precision NOT NULL CHECK(total_seconds>=0),
 last_seconds double precision NOT NULL CHECK(last_seconds>=0 AND last_seconds<=86400),
 max_seconds double precision NOT NULL CHECK(max_seconds>=0 AND max_seconds<=86400),
 updated_at timestamptz NOT NULL,
 PRIMARY KEY(bot_scope,name)
);
INSERT INTO schema_migrations(version) VALUES(5) ON CONFLICT DO NOTHING;
