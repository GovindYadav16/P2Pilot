# ADR 0004: Durable run state, offline evals, token-scoped MCP

Status: accepted (Sprint 2.5)

## Context

Sprint 2 left three gaps an operator (or interviewer) would hit first:

1. An invoice paused for approval lived in process memory. Restarting the API, or a
   second replica, lost the run while Postgres still said `in_review`.
2. "It works" rested on manual clicks. Nothing measured extraction accuracy or caught a
   change that made a risky invoice auto-post.
3. MCP tools took `tenant_id` as an argument and trusted it. Any MCP client, or an agent
   steered by injected text, could read another tenant's data by changing one string.

## Decisions

**Postgres checkpointer.** LangGraph's `PostgresSaver` stores run state in the same
database, in a separate `langgraph` schema. Supabase exposes only `public` through
PostgREST, and the migration plus startup code revoke everything on `langgraph` from
`anon` and `authenticated`, because checkpoints contain invoice text. We wrap the *sync*
saver and run its calls in a thread instead of using `AsyncPostgresSaver`: psycopg's
async mode does not work with Windows' default Proactor event loop, and the wrapper
behaves the same everywhere. `CHECKPOINTER=auto` uses Postgres when a database URL is set;
unit tests pin `memory`.

**Offline eval runner.** `python -m p2pilot.evals.invoice_eval` runs the 15 planted
invoices through the same graph the API uses, with tools backed by the seeded dataset in
memory (repeatable: a database-backed run would flag every file as a duplicate on the
second run). It scores extraction, decision and findings per invoice, writes JSON and
Markdown reports, and exits non-zero below a pass rate or on any *unsafe auto-post*
(a review case that was posted). That number is the release gate for money safety.

**Token-scoped MCP.** Tools no longer take `tenant_id`. The client sends the end user's
Supabase JWT in the MCP transport headers; the server verifies it and resolves tenant
and role from `memberships`, the same rules as the REST API. The token never appears in
tool arguments, so the model can neither see nor alter it. Missing or bad tokens get a
`forbidden` envelope (fail closed).

## Consequences

- A reviewer can approve hours later, on any API process, after restarts.
- The eval is the regression test for the whole skill (`tests/test_eval.py` runs it with
  the rules extractor) and the measuring stick for prompt or model changes (LLM mode).
- MCP clients must forward a user token; service-to-service calls without a user are not
  supported yet (would need a separate, scoped service credential).
- Cost per invoice is reported as tokens; currency cost is in LiteLLM's spend logs.
