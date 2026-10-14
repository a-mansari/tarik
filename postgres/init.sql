-- Files clients send Tarik in the chat. n8n stores them; the MCP tools read them by id.
create table if not exists tarik_files (
    id serial primary key,
    session_id text not null,
    file_name text not null,
    mime_type text not null,
    data bytea not null,
    invoice_id integer, -- the draft invoice made from it: a second call must not make another
    parent_id integer references tarik_files (id), -- set on each invoice cut out of a multi-invoice PDF
    kind text check (kind in ('invoice', 'statement')), -- set by identify_document, before anything is created
    statement_id integer, -- the bank statement imported from it: a second call must not import it again
    created_at timestamptz not null default now()
);

-- What check_invoice read on each scan, against what Experio captured. The correction step reads it back.
create table if not exists tarik_checks (
    invoice_id integer primary key,
    readable boolean not null,
    extracted jsonb not null,
    bad_fields jsonb not null,
    checked_at timestamptz not null default now()
);

-- What classify_invoice decided: achat, vente or rb (bank statement). The tiers and account steps read it.
create table if not exists tarik_classifications (
    invoice_id integer primary key,
    category text not null check (category in ('achat', 'vente', 'rb')),
    reason text not null,
    classified_at timestamptz not null default now()
);

-- Each step outcome of each processing run (n8n "Tarik - Traitement"), and the questions waiting for the client.
create table if not exists tarik_results (
    id serial primary key,
    run_id text not null,
    session_id text not null,
    file_id integer,
    invoice_id integer,
    step text not null,
    status text not null,
    message text not null,
    created_at timestamptz not null default now()
);
create table if not exists tarik_pending (
    invoice_id integer primary key,
    session_id text not null,
    step text not null,
    question text not null,
    created_at timestamptz not null default now()
);
