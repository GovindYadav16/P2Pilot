# ADR 0001: Postgres is the source of truth

- Status: Accepted
- Date: 2026-11-13

## Context

P2Pilot stores several kinds of state: tenants and permissions, P2P records (POs, receipts,
invoices), conversations, agent checkpoints, files, retrieval indexes and a vendor graph.
It must run on Azure, AWS and on-prem, so no single cloud service can be assumed.

## Options

1. Postgres as the system of record; Qdrant, Neo4j and object storage hold derived data or bytes.
2. Split records across services (e.g. conversations in a document DB, vendors in Neo4j).
3. A managed cloud-specific store (Cosmos DB, DynamoDB).

## Decision

Option 1. Every business fact has exactly one authoritative row in Postgres (Supabase).
Qdrant and Neo4j are rebuildable projections: an ingest or sync job can recreate them from
Postgres plus object storage. Agent checkpoints also live in Postgres (LangGraph PostgresSaver,
Sprint 2) so an approval interrupt can resume on any pod.

Tenant isolation is enforced with row-level security in the database, not only in API code.
The API forwards the user's JWT to PostgREST for user-scoped reads; background workers use the
service role with explicit `tenant_id` filters.

## Consequences

- One backup and migration story; projections can be dropped and rebuilt.
- RLS is the last line of defence even if an API handler has a bug (tested in
  `backend/tests/integration/test_rls.py`).
- Policies add query cost; membership checks use a `security definer` function and an index on
  `memberships(user_id)` to keep them cheap.
- Writes to Qdrant/Neo4j are eventually consistent with Postgres; retrieval may lag ingest by seconds.
