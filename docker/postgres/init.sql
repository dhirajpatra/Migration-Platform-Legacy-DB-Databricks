CREATE EXTENSION IF NOT EXISTS vector;          -- RAG / KB (used from milestone 2)
CREATE SCHEMA IF NOT EXISTS kb;                 -- vector collections
CREATE SCHEMA IF NOT EXISTS mig;                -- run state / audit
CREATE TABLE IF NOT EXISTS mig.audit (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  run_id TEXT NOT NULL,
  actor TEXT NOT NULL,           -- agent name or human id
  action TEXT NOT NULL,
  detail JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_run_idx ON mig.audit(run_id);
