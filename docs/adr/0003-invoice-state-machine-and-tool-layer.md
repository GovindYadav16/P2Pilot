# ADR 0003: Invoices run as a LangGraph state machine; tools share one envelope and two transports

- Status: Accepted
- Date: 2026-11-14

## Context

Invoice processing moves money. Every invoice must go through the same steps, the
arithmetic must be exact, and risky invoices need a human decision that can arrive minutes
or days later. The JD also asks for MCP tools with clear failure modes.

## Decisions

1. **Explicit state machine, not a ReAct loop.** `load_file → extract → save → match →
   checks → decide → (approval) → post | escalate → summarize`. The LLM is used for one
   step only (reading the PDF into a typed `InvoiceExtraction`). Matching, tolerances and
   the auto-approve rule are deterministic Python.
2. **Human-in-the-loop via `interrupt()`.** The `approval` node pauses the run; the API
   resumes it with `Command(resume=...)`. Only `approver`/`tenant_admin` may resume, and
   never the user who submitted the invoice (maker-checker).
3. **Prompt injection cannot approve anything.** Document text is wrapped as data in the
   extraction prompt, a detector flags instruction-like text (forcing review), and the
   decision is not made by the model at all.
4. **One tool envelope.** Every tool returns `{ok, data, error{code, retryable,
   user_message}}`. Failures are never empty results; internals never reach the user.
5. **Side effects are idempotent.** `erp_post_invoice` takes an idempotency key backed by a
   UNIQUE constraint, so a retried or replayed post cannot double-pay.
6. **Two transports, one implementation.** The state machine calls `P2PTools` in-process
   (it never needs tool discovery). The same functions are exposed over MCP
   (`p2pilot.mcp_server`) for agents that choose tools dynamically (Sprint 3) and for
   inspection with the MCP Inspector.

## Consequences

- Behaviour is testable without a model or database (`tests/fakes.py`).
- ~~Known limitation: paused runs live in an in-process `MemorySaver`.~~ Resolved in
  Sprint 2.5 by a Postgres checkpointer (ADR 0004).
- ~~Known limitation: the MCP server trusts its caller's `tenant_id`.~~ Resolved in
  Sprint 2.5: identity comes from the caller's token (ADR 0004).
