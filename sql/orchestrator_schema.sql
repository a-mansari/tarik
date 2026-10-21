-- name: orchestrator_schema
-- The orchestrator's own tables, in schema orchestrator (made at each start of the server: idempotent).
create schema if not exists orchestrator;

-- === Runs and their steps (engine/runs.py), and how the team arranged each canvas (app/layouts.py) ===
create table if not exists runs (
    id uuid primary key,
    graph text not null,
    cabinet text not null,
    session_id text not null,
    thread_id text,
    status text not null,  -- queued, running, done, error
    input jsonb not null,
    output jsonb,
    error text,
    started_at timestamptz not null default now(),
    ended_at timestamptz
);
create index if not exists runs_started on runs (started_at desc);
create index if not exists runs_session on runs (cabinet, session_id);
create index if not exists runs_cabinet_started on runs (cabinet, started_at desc);  -- a cabinet's latest, its dashboard
-- A run started inside another's step (a workflow the agent runs, a thread handed to the agent): its parent, and the
-- parent's step it started from.
alter table runs add column if not exists parent_id uuid references runs (id);
alter table runs add column if not exists parent_node text;
create index if not exists runs_parent on runs (parent_id);
create table if not exists steps (
    id bigserial primary key,
    run_id uuid not null references runs (id),
    at timestamptz not null default now(),
    path text not null,  -- the subgraphs it sits in: "" at the top, "fichier|facture" inside
    node text not null,
    status text not null,  -- the step's own: ok, question, stop, wait, error, or "" for plumbing
    summary text not null,  -- one line for the list
    detail jsonb  -- the node's whole update
);
create index if not exists steps_run on steps (run_id, id);
-- Which run of its part a step belongs to: the subgraphs' task ids ("fil:<id>|..."), so two invoices or two email
-- threads of one run, side by side, each keep their own order of steps (the canvas follows it).
alter table steps add column if not exists lane text;
-- Its round in its graph (LangGraph's superstep): a step follows a step of the round before, in its lane. Parallel
-- branches (an answer and the files at once) each keep their own order.
alter table steps add column if not exists round integer;
create table if not exists layouts (
    graph text primary key,
    layout jsonb not null,  -- {nodes: {id: {x, y}}, edges: {id: {points: [{x, y}], style}}}: how the team arranged it
    updated_at timestamptz not null default now()
);

-- === The team's accounts and sessions (app/accounts.py) ===
create table if not exists users (
    email text primary key,
    name text not null,
    password text not null,  -- scrypt$<salt hex>$<hash hex>
    active boolean not null default true,
    created_at timestamptz not null default now()
);
-- What the account may do: an admin changes what everyone uses (content, cabinets' settings, canvases' layout) and
-- starts runs by hand; a member reads everything and writes to Tarik on the Discussion page.
alter table users add column if not exists role text not null default 'member' check (role in ('admin', 'member'));
-- An account made from the Comptes page has no password until its person sets one, through its invitation.
alter table users alter column password drop not null;
-- A one-time link to set an account's password (a new account's, or a reset): only its token's hash is kept.
create table if not exists invitations (
    token text primary key,  -- sha256 of the link's token
    email text not null references users (email) on delete cascade,
    expires_at timestamptz not null,
    used_at timestamptz
);
create table if not exists sessions (
    token text primary key,  -- sha256 of the cookie's token
    email text not null references users (email) on delete cascade,
    expires_at timestamptz not null
);

-- === Every version of the content the team edits (engine/content.py) ===
create table if not exists content_versions (
    id bigserial primary key,
    path text not null,
    text text not null,
    author text not null,
    note text not null default '',
    created_at timestamptz not null default now()
);
create index if not exists content_versions_path on content_versions (path, id desc);

-- === Every change made to a cabinet's settings from the Cabinets page (app/cabinet_settings.py) ===
-- The values live in Vault only; here who changed which, when and why. A secret's value is never kept, not even here.
create table if not exists settings_changes (
    id bigserial primary key,
    cabinet text not null,
    author text not null,
    note text not null default '',
    changes jsonb not null,  -- [{key, before, after}]: before/after null for a secret
    vault_version integer not null,
    created_at timestamptz not null default now()
);
create index if not exists settings_changes_cabinet on settings_changes (cabinet, id desc);
-- Each cabinet's model (MODEL_ADAPTER, the Cabinets page): not a secret, so not in Vault; here, outside the cabinets'
-- schemas, no tool reads it, nor the agent.
create table if not exists cabinet_models (
    cabinet text primary key,
    adapter text not null,  -- a key of tarik.core.model.ADAPTERS
    updated_at timestamptz not null default now()
);

-- === Who did what in the UI (app/audit.py): the Journal page, an admin's ===
create table if not exists audit (
    id bigserial primary key,
    at timestamptz not null default now(),
    actor text not null,  -- an account's email, "terminal" (users.py), "script" (TARIK_GRAPH_TOKEN), or the email typed
    action text not null,
    target text not null default '',
    detail jsonb not null default '{}'
);
create index if not exists audit_at on audit (id desc);
