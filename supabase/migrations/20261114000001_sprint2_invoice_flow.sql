-- Sprint 2: invoice decisions and reviews, idempotent ERP postings, exception tickets.

alter table public.invoices
    add column decision       text check (decision in ('auto_post', 'review')),
    add column findings       jsonb not null default '[]'::jsonb,
    add column reviewed_by    uuid references auth.users (id),
    add column reviewed_at    timestamptz,
    add column review_comment text,
    add column thread_id      text;   -- agent run (LangGraph thread) that processed it

create index on public.invoices (tenant_id, status);

-- Mock ERP ledger. idempotency_key is UNIQUE, so a retried "post" can never double-post.
create table public.erp_postings (
    id                  uuid primary key default gen_random_uuid(),
    tenant_id           uuid not null references public.tenants (id) on delete cascade,
    invoice_id          uuid not null references public.invoices (id) on delete cascade,
    idempotency_key     text not null unique,
    erp_document_number text not null,
    posted_by           uuid references auth.users (id),
    posted_at           timestamptz not null default now()
);

-- ConnectWise-style exception tickets (mock ticketing system).
create sequence public.ticket_number_seq;
create table public.tickets (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid not null references public.tenants (id) on delete cascade,
    number      text not null unique
                default 'TCK-' || lpad(nextval('public.ticket_number_seq')::text, 5, '0'),
    invoice_id  uuid references public.invoices (id) on delete set null,
    title       text not null,
    body        text not null,
    priority    text not null default 'medium' check (priority in ('low', 'medium', 'high')),
    status      text not null default 'open' check (status in ('open', 'in_progress', 'closed')),
    created_by  uuid references auth.users (id),
    created_at  timestamptz not null default now()
);
create index on public.tickets (tenant_id, created_at);

alter table public.erp_postings enable row level security;
alter table public.tickets      enable row level security;

create policy erp_postings_member_read on public.erp_postings
    for select to authenticated using (app.is_member(tenant_id));
create policy tickets_member_read on public.tickets
    for select to authenticated using (app.is_member(tenant_id));
