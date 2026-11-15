# P2Pilot

An agentic procure-to-pay assistant for a manufacturing/distribution company. AP clerks upload
supplier invoices; the agent extracts, matches them against POs and goods receipts, checks
procurement policy with cited sources, scores vendor risk, and either posts to the ERP or raises
an exception ticket, with a human approval step.

Portfolio project. All data is synthetic.

**Stack:** LangGraph + Deep Agents · FastAPI (SSE) · MCP tools · LiteLLM gateway (Azure OpenAI /
Anthropic / OpenAI) · Supabase (Postgres + RLS, Auth, Storage, PostgREST) · Qdrant hybrid
retrieval · Neo4j · React + TypeScript + Vite · pytest / Vitest · Docker / Kubernetes.

## Status

| Sprint | Scope | State |
|---|---|---|
| 1 | Walking skeleton: schema + RLS, synthetic data, streaming chat through the gateway, login | done |
| 2 | Invoice skill: upload, extraction, 3-way match, MCP tools, approval interrupt | done |
| 2.5 | Hardening: Postgres checkpoints (approvals survive restarts), eval runner, MCP auth from token | done |
| 3 | Policy RAG (Qdrant hybrid + rerank + citations), Neo4j risk graph, research Deep Agent | planned |
| 4 | Admin Builder, Sandbox, entitlements, eval-gated deploy, Helm + Azure DevOps | planned |

## Repository layout

```
backend/            Python package `p2pilot`
  p2pilot/api/        FastAPI app, auth dependency
  p2pilot/agents/     LangGraph graphs, LLM factory, SSE stream adapter
  p2pilot/sse.py      Streaming event contract (shared with web/src/types/events.ts)
  p2pilot/data_gen/   Synthetic data generator + seeder
  tests/              pytest (unit) and tests/integration (needs Supabase)
web/                React + Vite chat UI
supabase/migrations SQL schema and RLS policies
deploy/litellm/     Gateway configs per provider
docs/adr/           Architecture decision records
docs/stories/       User stories with acceptance criteria
```

## Run it locally (Windows, PowerShell)

Prerequisites: Docker Desktop, Python 3.11+, Node 20+.

### 1. Supabase (Postgres, Auth, PostgREST, Storage)

```powershell
cd E:\AI_ML\P2Pilot
npx supabase init          # once; answer "n" to the VS Code prompts
npx supabase start         # first run downloads images, takes a few minutes
npx supabase db reset      # applies supabase/migrations
npx supabase status        # copy the API URL, keys and DB URL into .env
```

### 2. Environment

```powershell
Copy-Item .env.example .env
Copy-Item web\.env.example web\.env
```

Fill `.env`: your LLM provider keys, `SUPABASE_ANON_KEY` and `SUPABASE_SERVICE_ROLE_KEY` from
`supabase status`. Set `SUPABASE_JWT_SECRET` only if `supabase status` prints a JWT secret;
otherwise leave it empty and tokens are verified with the stack's JWKS.
If you use Azure, also set your deployment names in `deploy/litellm/config.azure.yaml`.
Fill `web\.env` with the same Supabase URL and anon key.

No LLM key yet? Set `LLM_FAKE=true` and everything runs with a deterministic fake model.

### 3. Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest                                   # unit tests, no services needed
python -m p2pilot.data_gen.seed          # demo users + synthetic data
$env:P2PILOT_DB_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
pytest -m integration                    # RLS tests against local Supabase
```

### 4. Gateway and API

Either in Docker:

```powershell
cd ..
docker compose up -d --build             # litellm on :4000, api on :8000
```

or the API on your machine (handy for debugging) with only LiteLLM in Docker:

```powershell
docker compose up -d litellm
cd backend; uvicorn p2pilot.api.main:app --reload --port 8000
```

### 5. Web

```powershell
cd web
npm install
npm test
npm run dev                              # http://localhost:5173
```

Sign in as `clerk@acme.test` with the password from `SEED_USER_PASSWORD` (default `P2Pilot!dev123`).
Other demo users: `approver@`, `auditor@`, `admin@` at `acme.test` and `bharat.test`.

## Sprint 2: invoice processing

```powershell
cd backend
pip install -e ".[dev]"                      # new deps: pypdf, python-multipart, psycopg_pool, mcp, reportlab
cd .. ; npx supabase migration up ; cd backend   # applies the Sprint 2 migration
python -m p2pilot.data_gen.invoices          # 15 invoice PDFs + ground_truth.json in data/invoices
pytest ; pytest -m integration
python -m p2pilot.mcp_server.server          # optional: MCP tools on http://127.0.0.1:8100/mcp
```

In the web app: **Attach invoice** -> pick a PDF from `data/invoices`. Clean invoices post
automatically; exceptions pause with an approval card. Sign in as `approver@acme.test`, open
**Approvals**, and approve or reject (the submitter can never approve their own invoice).

## Sprint 2.5: hardening

```powershell
cd backend
pip install -e ".[dev]"                      # new dep: langgraph-checkpoint-postgres
cd .. ; npx supabase migration up ; cd backend   # private `langgraph` schema for run state
pytest ; pytest -m integration               # adds restart-and-resume + run-state privacy tests
python -m p2pilot.evals.invoice_eval --extractor rules   # pipeline only, no model
python -m p2pilot.evals.invoice_eval                     # real model via LiteLLM
```

- **Approvals survive restarts.** LangGraph checkpoints are stored in Postgres (schema
  `langgraph`, never exposed to the browser). `CHECKPOINTER=auto|postgres|memory`;
  `/health` shows which one is active. Try it: pause an invoice, restart uvicorn, approve.
- **Eval runner.** Runs all 15 planted invoices through the real state machine and scores
  extraction, decision and findings. Report in `data/evals/latest.md`; exits 1 below
  `--min-pass-rate` or on any unsafe auto-post, so it can gate CI.
- **MCP tools take identity from the token.** No more `tenant_id` arguments: send
  `Authorization: Bearer <user JWT>` (and `X-Tenant` if needed) on the MCP connection.

## Design notes

- [ADR 0001: Postgres is the source of truth](docs/adr/0001-postgres-source-of-truth.md)
- [ADR 0002: Attachments, artifacts and corpus are separate](docs/adr/0002-attachments-artifacts-corpus.md)
- [ADR 0003: Invoice state machine and tool layer](docs/adr/0003-invoice-state-machine-and-tool-layer.md)
- [ADR 0004: Durable run state, offline evals, token-scoped MCP](docs/adr/0004-durable-runs-evals-mcp-auth.md)
- Streaming contract: every event is a typed model in `backend/p2pilot/sse.py`. Errors are
  explicit `error` events; a stream always ends with `run_finished`.
- The app never names a provider model. It asks LiteLLM for `fast`, `reasoning` or `embed`.
