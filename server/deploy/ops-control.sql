-- OSBI authentication control plane. No billing or external key synchronization.
BEGIN;

CREATE TABLE IF NOT EXISTS ops.osbi_nets (
  id text PRIMARY KEY CHECK (id ~ '^[a-z][a-z0-9-]{0,63}$'),
  name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
  schemas text[] NOT NULL DEFAULT ARRAY['general']::text[],
  enabled boolean NOT NULL DEFAULT true,
  notes text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ops.osbi_keys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  key text NOT NULL UNIQUE DEFAULT ('osbi_' || encode(extensions.gen_random_bytes(32), 'hex')),
  key_hash text GENERATED ALWAYS AS (encode(extensions.digest(key, 'sha256'), 'hex')) STORED UNIQUE,
  net_id text NOT NULL REFERENCES ops.osbi_nets(id),
  customer_id text NOT NULL DEFAULT ('customer-' || gen_random_uuid()::text),
  nickname text NOT NULL DEFAULT '',
  schema_permissions text[] DEFAULT NULL,
  rpm integer NOT NULL DEFAULT 300 CHECK (rpm BETWEEN 0 AND 100000),
  expires_at timestamptz NOT NULL DEFAULT (now() + interval '1 year'),
  revoked boolean NOT NULL DEFAULT false,
  notes text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK (key ~ '^osbi_[A-Za-z0-9_-]{24,128}$'),
  CHECK (length(btrim(customer_id)) BETWEEN 1 AND 200),
  CHECK (length(nickname) <= 200)
);

CREATE OR REPLACE FUNCTION ops.osbi_control_guard()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE names text[]; item text; allowed text[];
BEGIN
  IF TG_TABLE_NAME = 'osbi_nets' THEN
    names := NEW.schemas;
    IF TG_OP = 'UPDATE' AND NEW.id IS DISTINCT FROM OLD.id THEN
      RAISE EXCEPTION 'Net identity is immutable';
    END IF;
    IF cardinality(names) = 0 THEN RAISE EXCEPTION 'A net requires at least one schema'; END IF;
  ELSE
    names := NEW.schema_permissions;
    IF TG_OP = 'UPDATE' AND (NEW.key IS DISTINCT FROM OLD.key OR NEW.net_id IS DISTINCT FROM OLD.net_id) THEN
      RAISE EXCEPTION 'Key and net identity are immutable; issue a separate key instead';
    END IF;
    SELECT schemas INTO allowed FROM ops.osbi_nets WHERE id = NEW.net_id;
    IF allowed IS NULL THEN RAISE EXCEPTION 'Unknown net'; END IF;
    IF names IS NOT NULL AND NOT names <@ allowed THEN
      RAISE EXCEPTION 'Schema permissions must belong to the selected net';
    END IF;
  END IF;
  IF names IS NOT NULL THEN
    IF coalesce(array_ndims(names), 1) <> 1 OR array_position(names, NULL) IS NOT NULL THEN
      RAISE EXCEPTION 'Schemas must be a one-dimensional array without nulls';
    END IF;
    FOREACH item IN ARRAY names LOOP
      IF item !~ '^[a-z][a-z0-9-]{0,63}$' THEN RAISE EXCEPTION 'Invalid schema name'; END IF;
    END LOOP;
    IF cardinality(names) <> (SELECT count(DISTINCT value) FROM unnest(names) value) THEN
      RAISE EXCEPTION 'Duplicate schema names';
    END IF;
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS osbi_net_guard ON ops.osbi_nets;
CREATE TRIGGER osbi_net_guard BEFORE INSERT OR UPDATE ON ops.osbi_nets
FOR EACH ROW EXECUTE FUNCTION ops.osbi_control_guard();
DROP TRIGGER IF EXISTS osbi_key_guard ON ops.osbi_keys;
CREATE TRIGGER osbi_key_guard BEFORE INSERT OR UPDATE ON ops.osbi_keys
FOR EACH ROW EXECUTE FUNCTION ops.osbi_control_guard();

INSERT INTO ops.osbi_nets(id,name,schemas) VALUES
  ('government','Government',ARRAY['policy','economy']),
  ('general','General',ARRAY['general'])
ON CONFLICT(id) DO NOTHING;

ALTER TABLE ops.osbi_nets ENABLE ROW LEVEL SECURITY;
ALTER TABLE ops.osbi_keys ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON ops.osbi_nets,ops.osbi_keys FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON ops.osbi_nets,ops.osbi_keys TO service_role;
DROP POLICY IF EXISTS service_role_osbi_nets ON ops.osbi_nets;
CREATE POLICY service_role_osbi_nets ON ops.osbi_nets TO service_role USING(true) WITH CHECK(true);
DROP POLICY IF EXISTS service_role_osbi_keys ON ops.osbi_keys;
CREATE POLICY service_role_osbi_keys ON ops.osbi_keys TO service_role USING(true) WITH CHECK(true);

CREATE OR REPLACE FUNCTION ops.osbi_authenticate(p_key_hash text)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
  SELECT jsonb_build_object(
    'id',k.id,'customer',k.customer_id,'netId',n.id,'netName',n.name,
    'netSchemas',n.schemas,
    'schemaGrants',CASE WHEN k.schema_permissions IS NULL THEN n.schemas ELSE
      ARRAY(SELECT s FROM unnest(k.schema_permissions) s WHERE s=ANY(n.schemas)) END,
    'rpm',k.rpm,'expires',extract(epoch FROM k.expires_at))
  FROM ops.osbi_keys k JOIN ops.osbi_nets n ON n.id=k.net_id
  WHERE k.key_hash=p_key_hash AND NOT k.revoked AND k.expires_at>now() AND n.enabled;
$$;

CREATE OR REPLACE FUNCTION ops.osbi_net_definitions()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
  SELECT coalesce(jsonb_agg(jsonb_build_object('id',id,'name',name,'schemas',schemas)
    ORDER BY id),'[]'::jsonb) FROM ops.osbi_nets WHERE enabled;
$$;

REVOKE ALL ON FUNCTION ops.osbi_authenticate(text),ops.osbi_net_definitions(),ops.osbi_control_guard()
  FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION ops.osbi_authenticate(text),ops.osbi_net_definitions() TO service_role;

COMMENT ON TABLE ops.osbi_nets IS 'Independent OSBI data networks. New schema names also require a deployed server definition.';
COMMENT ON TABLE ops.osbi_keys IS 'OSBI authentication only; no billing or external key synchronization.';
COMMENT ON COLUMN ops.osbi_keys.key IS 'Client Bearer key. Generated on insert; immutable. Keep private.';
COMMENT ON COLUMN ops.osbi_keys.key_hash IS 'Generated authentication lookup hash; do not edit.';
COMMENT ON COLUMN ops.osbi_keys.net_id IS 'Exactly one immutable network per key.';
COMMENT ON COLUMN ops.osbi_keys.schema_permissions IS 'NULL: all schemas currently enabled for this net. Empty array: no schema access. Otherwise list permitted schema names.';
COMMENT ON COLUMN ops.osbi_keys.rpm IS 'Requests per minute; 0 means unlimited.';
COMMENT ON COLUMN ops.osbi_keys.expires_at IS 'Required expiry; default one year. Checked on every authenticated request.';
COMMENT ON COLUMN ops.osbi_keys.revoked IS 'Disable a key immediately; no balance or billing is involved.';
COMMIT;
