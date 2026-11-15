# ADR 0002: Chat attachments, conversation artifacts and the knowledge corpus are separate

- Status: Accepted
- Date: 2026-11-13

## Context

Three kinds of file pass through the system and are easy to mix up:

| Kind | Example | Who should see it | How long it lives |
|---|---|---|---|
| Chat attachment | An invoice PDF a clerk uploads in chat | The uploader (and the run processing it) | Short retention, then deleted |
| Conversation artifact | The vendor-research report the agent writes | The conversation owner | Durable, versioned |
| Knowledge corpus | Procurement policy, contracts | Every member of the tenant | Durable, re-ingestable |

If an attachment were indexed into the shared corpus, one user's invoice (with bank details)
would become retrievable by every user in the tenant. If artifacts were kept only in agent
memory, users could not come back to them.

## Decision

- Bytes go to object storage under separate prefixes: `attachments/`, `artifacts/`, `corpus/`,
  behind one `ObjectStore` interface (Supabase Storage, S3, Azure Blob, MinIO).
- Postgres `files` holds the pointer and metadata with `kind` in
  `('attachment', 'artifact', 'corpus')`, plus owner, MIME type, size, SHA-256, and `expires_at`
  for attachments.
- RLS: users see their own files plus the tenant's `corpus` files. Users cannot create
  `corpus` rows; promotion to the corpus is an explicit admin action that runs an ingest job.
- The agent's virtual filesystem holds `FileRef` pointers, not bytes; content is fetched lazily
  when a tool reads it (hydrate by reference).

## Consequences

- Corpus content in Qdrant can be traced to a `files` row and re-ingested when chunking changes.
- Large files never bloat Postgres or the agent state.
- Two-step uploads (signed URL, then register) need a cleanup job for orphaned objects.
