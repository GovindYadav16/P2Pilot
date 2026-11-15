-- P2Pilot core schema: tenants, P2P records, conversations, files, skills, spend ledger.
-- Every tenant-owned table carries tenant_id and is protected by row-level security.
-- See docs/adr/0001-postgres-source-of-truth.md and 0002-attachments-artifacts-corpus.md.

create schema if not exists app;

create type app.member_role as enum ('ap_clerk', 'approver', 'auditor', 'tenant_admin');

-- ---------------------------------------------------------------- tenancy
create table public.tenants (
    id         uuid primary key default gen_random_uuid(),
    slug       text not null unique,
    name       text not null,
    created_at timestamptz not null default now()
);

create table public.memberships (
    tenant_id  uuid not null references public.tenants (id) on delete cascade,
    user_id    uuid not null references auth.users (id) on delete cascade,
    role       app.member_role not null,
    created_at timestamptz not null default now(),
    primary key (tenant_id, user_id)
);
create index on public.memberships (user_id);

-- SECURITY DEFINER so policies can consult memberships without recursing into its own RLS.
create function app.is_member(t uuid) returns boolean
language sql stable security definer set search_path = '' as $$
    select exists (
        select 1 from public.memberships m
        where m.tenant_id = t and m.user_id = (select auth.uid())
    );
$$;

create function app.has_role(t uuid, roles app.member_role[]) returns boolean
language sql stable security definer set search_path = '' as $$
    select exists (
        select 1 from public.memberships m
        where m.tenant_id = t and m.user_id = (select auth.uid()) and m.role = any (roles)
    );
$$;

grant usage on schema app to authenticated;
grant execute on function app.is_member(uuid), app.has_role(uuid, app.member_role[]) to authenticated;

-- ---------------------------------------------------------------- P2P records (mock ERP)
create table public.vendors (
    id                 uuid primary key default gen_random_uuid(),
    tenant_id          uuid not null references public.tenants (id) on delete cascade,
    name               text not null,
    gstin              text not null,
    state              text,
    category           text,
    parent_company     text,
    bank_account_hash  text not null,  -- hash only; raw account numbers never stored here
    created_at         timestamptz not null default now(),
    unique (tenant_id, gstin)
);

create table public.purchase_orders (
    id         uuid primary key default gen_random_uuid(),
    tenant_id  uuid not null references public.tenants (id) on delete cascade,
    vendor_id  uuid not null references public.vendors (id),
    po_number  text not null,
    status     text not null default 'open' check (status in ('open', 'closed', 'cancelled')),
    currency   text not null default 'INR',
    ordered_at date not null,
    created_at timestamptz not null default now(),
    unique (tenant_id, po_number)
);

create table public.po_lines (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid not null references public.tenants (id) on delete cascade,
    po_id       uuid not null references public.purchase_orders (id) on delete cascade,
    line_no     int not null,
    sku         text not null,
    description text not null,
    qty         numeric(14, 3) not null check (qty > 0),
    unit_price  numeric(14, 2) not null check (unit_price >= 0),
    tax_rate    numeric(5, 2) not null default 18.00,
    unique (po_id, line_no)
);

create table public.goods_receipts (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid not null references public.tenants (id) on delete cascade,
    po_id       uuid not null references public.purchase_orders (id) on delete cascade,
    grn_number  text not null,
    received_at date not null,
    unique (tenant_id, grn_number)
);

create table public.grn_lines (
    id           uuid primary key default gen_random_uuid(),
    tenant_id    uuid not null references public.tenants (id) on delete cascade,
    grn_id       uuid not null references public.goods_receipts (id) on delete cascade,
    po_line_id   uuid not null references public.po_lines (id),
    qty_received numeric(14, 3) not null check (qty_received >= 0)
);

-- ---------------------------------------------------------------- files
-- Bytes live in object storage; Postgres keeps the pointer and metadata.
-- kind separates the three lifecycles (ADR 0002): chat attachments, durable
-- conversation artifacts, and knowledge-corpus sources.
create table public.files (
    id          uuid primary key default gen_random_uuid(),
    tenant_id   uuid not null references public.tenants (id) on delete cascade,
    owner_id    uuid not null references auth.users (id),
    kind        text not null check (kind in ('attachment', 'artifact', 'corpus')),
    object_key  text not null unique,
    filename    text not null,
    mime_type   text not null,
    size_bytes  bigint not null check (size_bytes >= 0),
    sha256      text not null,
    created_at  timestamptz not null default now(),
    expires_at  timestamptz  -- set for attachments; null for artifacts and corpus
);

create table public.invoices (
    id             uuid primary key default gen_random_uuid(),
    tenant_id      uuid not null references public.tenants (id) on delete cascade,
    vendor_id      uuid references public.vendors (id),
    po_id          uuid references public.purchase_orders (id),
    file_id        uuid references public.files (id),
    invoice_number text,
    invoice_date   date,
    subtotal       numeric(14, 2),
    tax_total      numeric(14, 2),
    total          numeric(14, 2),
    status         text not null default 'received'
                   check (status in ('received', 'extracted', 'in_review', 'approved',
                                     'posted', 'rejected', 'exception')),
    extracted      jsonb not null default '{}'::jsonb,
    created_at     timestamptz not null default now()
);
create index on public.invoices (tenant_id, vendor_id, invoice_number);

-- ---------------------------------------------------------------- conversations
create table public.conversations (
    id         uuid primary key default gen_random_uuid(),
    tenant_id  uuid not null references public.tenants (id) on delete cascade,
    user_id    uuid not null references auth.users (id) on delete cascade,
    title      text,
    created_at timestamptz not null default now()
);

create table public.messages (
    id              uuid primary key default gen_random_uuid(),
    tenant_id       uuid not null references public.tenants (id) on delete cascade,
    conversation_id uuid not null references public.conversations (id) on delete cascade,
    role            text not null check (role in ('user', 'assistant', 'tool', 'system')),
    content         jsonb not null,
    created_at      timestamptz not null default now()
);
create index on public.messages (conversation_id, created_at);

-- ---------------------------------------------------------------- skills & entitlements
create table public.skills (
    id        uuid primary key default gen_random_uuid(),
    tenant_id uuid not null references public.tenants (id) on delete cascade,
    slug      text not null,
    name      text not null,
    unique (tenant_id, slug)
);

-- Versions are immutable: the Admin Builder creates a new version, never edits one.
create table public.skill_versions (
    id                 uuid primary key default gen_random_uuid(),
    tenant_id          uuid not null references public.tenants (id) on delete cascade,
    skill_id           uuid not null references public.skills (id) on delete cascade,
    version            int not null,
    system_prompt      text not null,
    tools              text[] not null default '{}',
    model_alias        text not null default 'fast' check (model_alias in ('fast', 'reasoning')),
    max_steps          int not null default 12,
    approval_threshold numeric(14, 2),
    status             text not null default 'draft' check (status in ('draft', 'live', 'retired')),
    created_by         uuid references auth.users (id),
    created_at         timestamptz not null default now(),
    unique (skill_id, version)
);
create unique index one_live_version_per_skill on public.skill_versions (skill_id)
    where status = 'live';

create table public.entitlements (
    tenant_id uuid not null references public.tenants (id) on delete cascade,
    role      app.member_role not null,
    skill_id  uuid not null references public.skills (id) on delete cascade,
    primary key (tenant_id, role, skill_id)
);

-- ---------------------------------------------------------------- spend ledger
create table public.llm_spend (
    id                bigint generated always as identity primary key,
    tenant_id         uuid not null references public.tenants (id) on delete cascade,
    user_id           uuid,
    skill             text,
    run_id            text,
    model             text not null,
    prompt_tokens     int not null default 0,
    completion_tokens int not null default 0,
    cost_usd          numeric(12, 6) not null default 0,
    created_at        timestamptz not null default now()
);
create index on public.llm_spend (tenant_id, created_at);

-- ================================================================ row-level security
alter table public.tenants         enable row level security;
alter table public.memberships     enable row level security;
alter table public.vendors         enable row level security;
alter table public.purchase_orders enable row level security;
alter table public.po_lines        enable row level security;
alter table public.goods_receipts  enable row level security;
alter table public.grn_lines       enable row level security;
alter table public.files           enable row level security;
alter table public.invoices        enable row level security;
alter table public.conversations   enable row level security;
alter table public.messages        enable row level security;
alter table public.skills          enable row level security;
alter table public.skill_versions  enable row level security;
alter table public.entitlements    enable row level security;
alter table public.llm_spend       enable row level security;

create policy tenants_read on public.tenants
    for select to authenticated using (app.is_member(id));

create policy memberships_read on public.memberships
    for select to authenticated using (app.is_member(tenant_id));
create policy memberships_admin on public.memberships
    for all to authenticated
    using (app.has_role(tenant_id, '{tenant_admin}'))
    with check (app.has_role(tenant_id, '{tenant_admin}'));

-- Mock-ERP records: any member may read. Writes come from the service role (tools, seeder).
do $$
declare t text;
begin
    foreach t in array array['vendors', 'purchase_orders', 'po_lines', 'goods_receipts',
                             'grn_lines', 'skills', 'skill_versions', 'entitlements']
    loop
        execute format(
            'create policy %1$s_member_read on public.%1$I for select to authenticated '
            'using (app.is_member(tenant_id))', t);
    end loop;
end $$;

-- Invoices: members read; clerks/approvers/admins create; approvers/admins change status.
create policy invoices_read on public.invoices
    for select to authenticated using (app.is_member(tenant_id));
create policy invoices_insert on public.invoices
    for insert to authenticated
    with check (app.has_role(tenant_id, '{ap_clerk,approver,tenant_admin}'));
create policy invoices_update on public.invoices
    for update to authenticated
    using (app.has_role(tenant_id, '{approver,tenant_admin}'))
    with check (app.has_role(tenant_id, '{approver,tenant_admin}'));

-- Files: you see your own files, plus shared corpus files of your tenant.
create policy files_read on public.files
    for select to authenticated
    using (app.is_member(tenant_id) and (owner_id = (select auth.uid()) or kind = 'corpus'));
create policy files_insert on public.files
    for insert to authenticated
    with check (app.is_member(tenant_id) and owner_id = (select auth.uid()) and kind <> 'corpus');

-- Conversations and messages are private to the user who owns the conversation.
create policy conversations_own on public.conversations
    for all to authenticated
    using (user_id = (select auth.uid()) and app.is_member(tenant_id))
    with check (user_id = (select auth.uid()) and app.is_member(tenant_id));

create policy messages_own on public.messages
    for all to authenticated
    using (exists (select 1 from public.conversations c
                   where c.id = conversation_id and c.user_id = (select auth.uid())))
    with check (exists (select 1 from public.conversations c
                        where c.id = conversation_id and c.user_id = (select auth.uid())
                          and c.tenant_id = messages.tenant_id));

-- Spend: admins and auditors only.
create policy llm_spend_read on public.llm_spend
    for select to authenticated using (app.has_role(tenant_id, '{tenant_admin,auditor}'));

-- Nothing is readable by the anon role: no policies grant it access.
