-- Operational metadata only. No material text, URLs, model output or graph facts.
BEGIN;
SET LOCAL lock_timeout = '5s';
CREATE TABLE work.osbi_jobs (
    net_id text NOT NULL,
    job_id text NOT NULL,
    job_type text NOT NULL CHECK (job_type IN ('selection','ontology','batch')),
    status text NOT NULL CHECK (status IN ('queued','running','succeeded','retrying',
                                         'failed','interrupted','rejected','partial')),
    phase text,
    node_ids text[] NOT NULL DEFAULT '{}',
    channel_id text,
    store_id text,
    material_id text,
    block_id text,
    batch_id text,
    trigger text,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    model_calls integer NOT NULL DEFAULT 0 CHECK (model_calls >= 0),
    selected_blocks integer NOT NULL DEFAULT 0,
    selected_links integer NOT NULL DEFAULT 0,
    entity_count integer NOT NULL DEFAULT 0,
    relation_count integer NOT NULL DEFAULT 0,
    total_jobs integer NOT NULL DEFAULT 0,
    completed_jobs integer NOT NULL DEFAULT 0,
    failed_jobs integer NOT NULL DEFAULT 0,
    duration_ms bigint,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (net_id, job_id)
);
CREATE INDEX osbi_jobs_recent ON work.osbi_jobs (updated_at DESC);
CREATE INDEX osbi_jobs_net_status ON work.osbi_jobs (net_id, status, updated_at DESC);
COMMENT ON TABLE work.osbi_jobs IS 'OSBI operational metadata for Studio; execution, original evidence and graph results remain on the OSBI server. Retained for 30 days after the last state change.';
COMMENT ON COLUMN work.osbi_jobs.node_ids IS 'Authenticated contributors; no bearer keys or customer secrets.';
COMMENT ON COLUMN work.osbi_jobs.phase IS 'Current stage, such as selection, ontology_entities or ontology_relations.';
COMMENT ON COLUMN work.osbi_jobs.error_code IS 'Bounded operational category; detailed errors stay in server logs.';
COMMENT ON COLUMN work.osbi_jobs.updated_at IS 'Last state transition, not export time. Used for 30-day retention.';
COMMENT ON COLUMN work.osbi_jobs.attempts IS 'Ontology attempt number; queued jobs have no completed attempt yet.';
ALTER TABLE work.osbi_jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON work.osbi_jobs FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON work.osbi_jobs TO service_role;
GRANT ALL ON work.osbi_jobs TO supabase_admin;
CREATE POLICY service_role_osbi_jobs ON work.osbi_jobs
    TO service_role USING (true) WITH CHECK (true);

CREATE FUNCTION work.cleanup_osbi_jobs()
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE removed bigint;
BEGIN
    DELETE FROM work.osbi_jobs WHERE updated_at < now() - interval '30 days';
    GET DIAGNOSTICS removed = ROW_COUNT;
    RETURN removed;
END;
$$;
REVOKE ALL ON FUNCTION work.cleanup_osbi_jobs() FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION work.cleanup_osbi_jobs() TO service_role;
-- pg_cron uses GMT: 17:17 UTC is 01:17 Asia/Shanghai, before the 01:18 batch.
SELECT cron.schedule('work_cleanup_osbi_jobs', '17 17 * * *',
                     'SELECT work.cleanup_osbi_jobs();');
NOTIFY pgrst, 'reload schema';
COMMIT;
