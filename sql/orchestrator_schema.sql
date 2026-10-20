-- name: orchestrator_schema
-- The orchestrator's own tables, in schema orchestrator (made at each start of the server: idempotent).
create schema if not exists orchestrator;

-- === Runs and their steps (engine/runs.py), and how the team arranged each canvas (engine/layouts.py) ===
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

-- === The team's accounts and sessions (engine/accounts.py) ===
create table if not exists users (
    email text primary key,
    name text not null,
    password text not null,  -- scrypt$<salt hex>$<hash hex>
    active boolean not null default true,
    created_at timestamptz not null default now()
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
