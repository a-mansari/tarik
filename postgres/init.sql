-- Files clients send Tarik in the chat. n8n stores them; the MCP tools read them by id.
create table if not exists tarik_files (
    id serial primary key,
    session_id text not null,
    file_name text not null,
    mime_type text not null,
    data bytea not null,
    invoice_id integer, -- the draft invoice made from it: a second call must not make another
    parent_id integer references tarik_files (id), -- set on each invoice cut out of a multi-invoice PDF
    kind text check (kind in ('invoice', 'statement', 'entries')), -- identify_document, before anything is created; entries: import_entries
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
    dossier_id integer, -- the dossier a setup step worked on
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

-- Tarik's mailbox, read over IMAP without changing it: where the reading stopped, each email
-- thread (one conversation, its own session), and each email received in it.
create table if not exists tarik_mail_state (
    folder text primary key,
    uidvalidity bigint not null,
    last_uid bigint not null -- emails up to this one were read; the first run starts here, skipping the backlog
);
create table if not exists tarik_mail_threads (
    thread_id text primary key, -- the Message-ID of the thread's first email
    session_id text not null unique, -- the conversation's session: memory, files and questions
    client_address text not null,
    subject text not null,
    state text not null default 'new' check (state in ('new', 'in_progress', 'waiting', 'done', 'ignored', 'error')),
    reason text, -- why it is ignored or in error
    attempts integer not null default 0, -- runs that did not finish: after 3 the thread is set aside in error
    claimed_at timestamptz, -- a run working on it: another one leaves it alone for 30 minutes
    updated_at timestamptz not null default now()
);
create table if not exists tarik_mail_messages (
    message_id text primary key,
    thread_id text not null references tarik_mail_threads (thread_id),
    uid bigint not null,
    from_address text not null,
    subject text not null,
    body text not null, -- the client's own text, quoted history removed
    references_ids text not null default '', -- its References, for the reply's
    handled boolean not null default false, -- answered by Tarik
    received_at timestamptz not null default now()
);
-- When the cabinet's manager was told of a refused sender: at most once a day per sender.
create table if not exists tarik_mail_alerts (
    id serial primary key,
    sender text not null,
    alerted_at timestamptz not null default now()
);
