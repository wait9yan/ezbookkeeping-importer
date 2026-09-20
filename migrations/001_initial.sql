CREATE TABLE IF NOT EXISTS schema_versions(version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());
INSERT INTO schema_versions(version) VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS cursors(
 source_id text NOT NULL, folder text NOT NULL, validity text NOT NULL,
 scan_upper bigint NOT NULL, scanned_uid bigint NOT NULL DEFAULT 0, historical_complete boolean NOT NULL DEFAULT false,
 checked_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(source_id,folder));
CREATE TABLE IF NOT EXISTS downloads(
 id bigserial PRIMARY KEY, source_id text NOT NULL, folder text NOT NULL, validity text NOT NULL, uid bigint NOT NULL,
 status text NOT NULL DEFAULT 'pending', message_id text, error text,
 UNIQUE(source_id,folder,validity,uid));
CREATE TABLE IF NOT EXISTS messages(
 id text PRIMARY KEY, evidence_path text NOT NULL, origin text NOT NULL, status text NOT NULL DEFAULT 'pending',
 source_status text NOT NULL DEFAULT 'requires_acceptance', source_reason text NOT NULL,
 parsed jsonb, accepted boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS reports(
 report_key text PRIMARY KEY, kind text NOT NULL, message_id text NOT NULL REFERENCES messages(id),
 fingerprint text NOT NULL, parsed jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS transactions(
 id text PRIMARY KEY, report_key text NOT NULL REFERENCES reports(report_key), row_key text NOT NULL, facts jsonb NOT NULL,
 version integer NOT NULL DEFAULT 1, state text NOT NULL DEFAULT 'pending', marker text NOT NULL UNIQUE,
 target_id text UNIQUE, decision jsonb, settlement jsonb,
 UNIQUE(report_key,row_key));
CREATE TABLE IF NOT EXISTS transaction_evidence(
 transaction_id text NOT NULL REFERENCES transactions(id), message_id text NOT NULL REFERENCES messages(id),
 row_key text NOT NULL, PRIMARY KEY(transaction_id,message_id,row_key));
CREATE TABLE IF NOT EXISTS jobs(
 id bigserial PRIMARY KEY, transaction_id text REFERENCES transactions(id), kind text NOT NULL,
 version integer NOT NULL DEFAULT 1, operation_key text NOT NULL UNIQUE, payload jsonb NOT NULL DEFAULT '{}',
 status text NOT NULL DEFAULT 'queued', target_id text, error text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS active_sync ON jobs(kind) WHERE kind='sync' AND status IN ('queued','dispatching');
CREATE UNIQUE INDEX IF NOT EXISTS active_write ON jobs(transaction_id) WHERE kind IN ('create','settle_amount') AND status IN ('queued','dispatching','unknown');
CREATE TABLE IF NOT EXISTS write_attempts(
 id bigserial PRIMARY KEY, job_id bigint NOT NULL REFERENCES jobs(id), request jsonb NOT NULL,
 outcome text NOT NULL, response jsonb, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS audit_events(
 id bigserial PRIMARY KEY, event text NOT NULL, entity_id text NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS issues(
 id bigserial PRIMARY KEY, code text NOT NULL, entity_id text NOT NULL, data jsonb NOT NULL,
 resolved boolean NOT NULL DEFAULT false, updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(code,entity_id));
CREATE TABLE IF NOT EXISTS reconciliation_items(
 report_key text NOT NULL REFERENCES reports(report_key), row_key text NOT NULL, status text NOT NULL,
 data jsonb NOT NULL, observed_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(report_key,row_key));
