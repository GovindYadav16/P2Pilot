-- Sprint 2.5: private schema for LangGraph run state (checkpoints).
--
-- The API's PostgresSaver creates its own tables here on first start (checkpoints,
-- checkpoint_blobs, checkpoint_writes, checkpoint_migrations). Run state contains invoice
-- text and reviewer decisions, so it must never be reachable from the browser:
--   * the schema is not in PostgREST's exposed schemas (only public is), and
--   * anon / authenticated get no privileges on it, now or for tables created later.
-- Only the server-side connection (P2PILOT_DB_URL) reads and writes it.

create schema if not exists langgraph;

revoke all on schema langgraph from public, anon, authenticated;
alter default privileges in schema langgraph revoke all on tables from public, anon, authenticated;
alter default privileges in schema langgraph revoke all on sequences from public, anon, authenticated;

comment on schema langgraph is
  'LangGraph checkpoints (paused and finished agent runs). Server-only; never exposed via PostgREST.';
