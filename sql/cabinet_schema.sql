-- name: cabinet_schema
-- One cabinet's tables, created in its own schema (tarik.core.cabinets.create_schemas, at each
-- start of the orchestrator): idempotent, so a new cabinet or a new table only needs a restart.

-- Files clients send Tarik (chat, email); the tools read them by id.
create table if not exists tarik_files (
    id serial primary key,
    session_id text not null,
    file_name text not null,
    mime_type text not null,
    data bytea not null,
    invoice_id integer, -- the draft invoice made from it: a second call must not make another
    parent_id integer references tarik_files (id), -- set on each invoice cut out of a multi-invoice PDF
    -- identify_document, before anything is created; entries: import_entries took it (imported or refused)
    kind text check (kind in ('invoice', 'statement', 'entries')),
    statement_id integer, -- the bank statement imported from it: a second call must not import it again
    created_at timestamptz not null default now()
);
-- The agent's attachment id: its files are saved once, however many times the sequence runs.
alter table tarik_files add column if not exists source_id text;
create unique index if not exists tarik_files_source on tarik_files (session_id, source_id);

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

-- Each step outcome of each processing run (engine/traitement.py, engine/dossier.py).
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
    choices jsonb, -- {id: name} a question proposed, for the client's answer
    created_at timestamptz not null default now()
);
-- A dossier whose entries were imported and whose onboarding is not finished yet (finish_setup waits on
-- Experio): an email follow-up finishes it when the client's run could not.
create table if not exists tarik_setups (
    dossier_id integer primary key,
    session_id text not null,
    created_at timestamptz not null default now()
);
-- A step outcome the client needn't read (tarik.core.app.routine): left out of the run's summary.
alter table tarik_results add column if not exists routine boolean not null default false;
alter table tarik_results add column if not exists choices jsonb;
alter table tarik_results add column if not exists journal_id integer; -- the bank journal a question (ask_banks) is about
-- The questions waiting for the client, one per invoice.
create table if not exists tarik_pending (
    invoice_id integer primary key,
    session_id text not null,
    step text not null,
    question text not null,
    choices jsonb, -- {id: name} the question proposed
    created_at timestamptz not null default now()
);
alter table tarik_pending add column if not exists choices jsonb;
-- Every invoice reaching hold_posting: it stays a verified draft until the client explicitly says to post it
-- (released), and the choice is kept for later runs, which don't carry their answer.
create table if not exists tarik_held (
    invoice_id integer primary key,
    session_id text not null
);
alter table tarik_held add column if not exists released boolean not null default false;

-- The client's oui / non answer to a step's question on an invoice (non-recoverable TVA, prepaid spread): a "non"
-- changes nothing in Experio, so without it here an invoice taken up again would be asked again.
create table if not exists tarik_answers (
    invoice_id integer not null,
    step text not null,
    answer text not null,
    primary key (invoice_id, step)
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
    state text not null default 'new', -- one of tarik_mail_threads_state_check's, below
    reason text, -- why it is ignored or in error
    attempts integer not null default 0, -- runs that did not finish: after 3 the thread is set aside in error
    claimed_at timestamptz, -- a run working on it: another one leaves it alone for 30 minutes
    updated_at timestamptz not null default now()
);
-- follow_up: Tarik answered, and the conversation's drafts Experio was still reading are taken up again,
-- their outcome sent in the thread, until follow_up_until.
alter table tarik_mail_threads add column if not exists follow_up_until timestamptz;
-- When Tarik told the client their new thread was received (acknowledge_thread): once per thread.
alter table tarik_mail_threads add column if not exists acknowledged_at timestamptz;
-- The states a thread may be in: dropped and added again at each start, so a new state applies to old schemas too.
alter table tarik_mail_threads drop constraint if exists tarik_mail_threads_state_check;
alter table tarik_mail_threads add constraint tarik_mail_threads_state_check
    check (state in ('new', 'in_progress', 'waiting', 'follow_up', 'done', 'ignored', 'error'));
create table if not exists tarik_mail_messages (
    message_id text primary key,
    thread_id text not null references tarik_mail_threads (thread_id),
    uid bigint not null,
    from_address text not null,
    subject text not null,
    body text not null, -- the client's own text, quoted history removed
    references_ids text not null default '', -- its References, for the reply's
    handled boolean not null default false, -- answered by Tarik, or refused (never to be answered)
    received_at timestamptz not null default now()
);
-- Where the sender asked for replies (Reply-To): used only when it is on the whitelist, never as who wrote.
alter table tarik_mail_messages add column if not exists reply_to text;
-- What a zip of the email held that Tarik does not take, and why (tarik.core.archives): told to the client.
alter table tarik_mail_messages add column if not exists skipped text[] not null default '{}';
-- When the cabinet's manager was told of a refused sender: at most once a day per sender.
create table if not exists tarik_mail_alerts (
    id serial primary key,
    sender text not null,
    alerted_at timestamptz not null default now()
);

-- The client's own messages, word for word, as the agent got them (tarik.core.conversation):
-- what the client wrote earlier in a conversation, never the agent's words.
create table if not exists tarik_client_messages (
    id serial primary key,
    session_id text not null,
    message text not null,
    created_at timestamptz not null default now()
);
create index if not exists tarik_client_messages_session on tarik_client_messages (session_id);

-- What the cabinet's agent was sent at each model call (engine/prompts.py; shown on the UI's Prompts page).
-- A call holds only the hashes of its system text, its tools and its messages: each distinct one is kept once in
-- tarik_prompt_parts, so a conversation's growing history is not stored again at every call, and the viewer
-- builds the full prompt from them. TARIK_PROMPTS_KEEP_DAYS days of calls are kept.
drop table if exists tarik_prompts; -- the first version, one full copy per call
create table if not exists tarik_prompt_parts (
    hash text primary key,
    content jsonb not null, -- a system text, the tools' names and descriptions, or one message (files left out)
    created_at timestamptz not null default now()
);
create table if not exists tarik_prompt_calls (
    id serial primary key,
    session_id text not null,
    agent text not null,
    system_hash text not null,
    tools_hash text not null,
    message_hashes text[] not null, -- in order
    created_at timestamptz not null default now()
);
create index if not exists tarik_prompt_calls_session on tarik_prompt_calls (session_id, id);
