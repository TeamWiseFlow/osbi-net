-- Add actual provider/model and API-reported token usage to Studio metadata.
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE work.osbi_jobs
    ADD COLUMN provider text,
    ADD COLUMN model text,
    ADD COLUMN api_calls integer NOT NULL DEFAULT 0 CHECK (api_calls >= 0),
    ADD COLUMN prompt_tokens bigint CHECK (prompt_tokens >= 0),
    ADD COLUMN completion_tokens bigint CHECK (completion_tokens >= 0),
    ADD COLUMN total_tokens bigint CHECK (total_tokens >= 0),
    ADD COLUMN usage_complete boolean NOT NULL DEFAULT false,
    ADD COLUMN model_usage jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(model_usage) = 'array');
COMMENT ON COLUMN work.osbi_jobs.provider IS 'Provider hostname of the latest observed HTTP attempt; no URL credentials, paths or queries.';
COMMENT ON COLUMN work.osbi_jobs.model IS 'Actual response model, or requested model if not reported, for the latest observed HTTP attempt.';
COMMENT ON COLUMN work.osbi_jobs.api_calls IS 'Observed HTTP attempts, including transport retries; model_calls counts logical model invocations.';
COMMENT ON COLUMN work.osbi_jobs.prompt_tokens IS 'Sum of known API input tokens across observed attempts. NULL means unknown; may be partial when usage_complete=false.';
COMMENT ON COLUMN work.osbi_jobs.completion_tokens IS 'Sum of known API output tokens across observed attempts, including rejected model output.';
COMMENT ON COLUMN work.osbi_jobs.total_tokens IS 'Sum of API totals, or exact input+output sums when both exist. Never estimated.';
COMMENT ON COLUMN work.osbi_jobs.usage_complete IS 'True only when all attempts are observed and all token components are known. Historical records remain false.';
COMMENT ON COLUMN work.osbi_jobs.model_usage IS 'Per-HTTP-attempt provider/model/stage/status/token metadata only. Ontology totals span retries; batch rows aggregate their own attempts and must not be added again to child-job totals.';
NOTIFY pgrst, 'reload schema';
COMMIT;
