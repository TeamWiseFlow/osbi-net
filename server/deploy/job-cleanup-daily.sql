-- Run Studio cleanup daily at 01:17 Asia/Shanghai, before the 01:18 batch.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15s';

DO $$
BEGIN
    IF current_setting('cron.timezone') NOT IN ('GMT', 'UTC', 'Etc/UTC') THEN
        RAISE EXCEPTION 'Expected a UTC pg_cron scheduler; convert 01:17 Asia/Shanghai before applying';
    END IF;
    IF (SELECT count(*) FROM cron.job WHERE jobname='work_cleanup_osbi_jobs') <> 1 THEN
        RAISE EXCEPTION 'Expected exactly one work_cleanup_osbi_jobs cron job';
    END IF;
END;
$$;

-- 17:17 UTC is 01:17 Asia/Shanghai on the following calendar day.
SELECT cron.alter_job(
    (SELECT jobid FROM cron.job WHERE jobname='work_cleanup_osbi_jobs'),
    schedule => '17 17 * * *'
);
COMMIT;
