-- Extend Studio job retention; keep the existing cleanup schedule and grants.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15s';

CREATE OR REPLACE FUNCTION work.cleanup_osbi_jobs()
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE removed bigint;
BEGIN
    DELETE FROM work.osbi_jobs WHERE updated_at < now() - interval '30 days';
    GET DIAGNOSTICS removed = ROW_COUNT;
    RETURN removed;
END;
$$;

COMMENT ON TABLE work.osbi_jobs IS 'OSBI operational metadata for Studio; execution, original evidence and graph results remain on the OSBI server. Retained for 30 days after the last state change.';
COMMENT ON COLUMN work.osbi_jobs.updated_at IS 'Last state transition, not export time. Used for 30-day retention.';
COMMIT;
