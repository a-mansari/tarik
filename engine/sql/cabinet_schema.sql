-- name: cabinet_schema
-- One cabinet's tables, created in its own schema (engine.tools.core.cabinets.create_schemas, at each
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
    kind text, -- invoice, statement, entries or other (tarik_files_kind_check, below)
    statement_id integer, -- the bank statement imported from it: a second call must not import it again
    created_at timestamptz not null default now()
);
-- The agent's attachment id: its files are saved once, however many times the sequence runs.
alter table tarik_files add column if not exists source_id text;
create unique index if not exists tarik_files_source on tarik_files (session_id, source_id);
-- The file's content hash, kept by Postgres: a statement sent again is found by it, never by hashing every file.
alter table tarik_files add column if not exists content_md5 text generated always as (md5(data)) stored;
create index if not exists tarik_files_statement_content on tarik_files (content_md5) where statement_id is not null;
-- A file's kinds: dropped and added again at each start, so a new kind applies to old schemas too.
alter table tarik_files drop constraint if exists tarik_files_kind_check;
alter table tarik_files add constraint tarik_files_kind_check
    check (kind in ('invoice', 'statement', 'entries', 'other', 'certificate', 'folder'));
-- folder: a file the client sent as an import folder's documents, made into that exp.import.folder (folder_id).
alter table tarik_files add column if not exists folder_id integer;
-- certificate: a supplier's attestation de regularite fiscale the client sent to answer settle_withholding's question,
-- uploaded to Experio as that record (exp.partner.regularity.form): never an invoice nor a statement.
alter table tarik_files add column if not exists regularity_id integer;
-- The same invoice file sent again (another email, the same zip twice): the draft already made from it, none made.
alter table tarik_files add column if not exists twin_of integer;
create index if not exists tarik_files_invoice_content on tarik_files (content_md5) where invoice_id is not null;
-- A TPE statement (exp.monetary.statement, not a bank statement): statement_id is one, of this network (cmi, naps...).
alter table tarik_files add column if not exists tpe_type text;

-- The files Tarik makes for a conversation (an export: Experio keeps none), sent with its next email reply and
-- downloadable from the conversation.
create table if not exists tarik_outbox (
    id serial primary key,
    session_id text not null,
    file_name text not null,
    mime_type text not null,
    data bytea not null,
    sent_at timestamptz,
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

-- What classify_invoice decided: achat, vente, rb (bank statement) or autre (neither). The tiers and account steps
-- read it.
create table if not exists tarik_classifications (
    invoice_id integer primary key,
    category text not null,
    reason text not null,
    classified_at timestamptz not null default now()
);
alter table tarik_classifications drop constraint if exists tarik_classifications_category_check;
alter table tarik_classifications add constraint tarik_classifications_category_check
    check (category in ('achat', 'vente', 'rb', 'autre'));

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
-- A step outcome the client needn't read (engine.tools.core.app.routine): left out of the run's summary.
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

-- A file the client asked to post (comptabiliser) when sending it: kept, because a question on the way (which bank?)
-- ends the run and the answer that resumes it does not carry the request.
alter table tarik_files add column if not exists post_asked boolean not null default false;

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
-- A routine's email was kept here before it became Tarik's own turn in its conversation (agent.began): gone.
alter table tarik_mail_threads drop column if exists opened_with;
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
-- What a zip of the email held that Tarik does not take, and why (engine.tools.core.archives): told to the client.
alter table tarik_mail_messages add column if not exists skipped text[] not null default '{}';
-- Tarik's own emails in each thread (its acknowledgement, its replies): a client's reply that names only one of them
-- (In-Reply-To without the earlier References) still finds its thread.
create table if not exists tarik_mail_sent (
    message_id text primary key,
    thread_id text not null references tarik_mail_threads (thread_id)
);
-- When the cabinet's manager was told of a refused sender: at most once a day per sender.
create table if not exists tarik_mail_alerts (
    id serial primary key,
    sender text not null,
    alerted_at timestamptz not null default now()
);

-- The client's own messages, word for word, as the agent got them (engine.tools.core.conversation):
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

-- Each model call's tokens and cost, the agent's and the tools' (engine/tools/core/usage.py), for the dashboard.
create table if not exists tarik_model_usage (
    id serial primary key,
    model text not null, -- as the provider names it: MiniMax-M3, claude-sonnet-...
    input_tokens integer not null, -- read fresh, cache writes included
    cached_tokens integer not null, -- read from the provider's cache
    output_tokens integer not null, -- thinking included
    cost numeric, -- US dollars; null for a model without a known price
    created_at timestamptz not null default now()
);
create index if not exists tarik_model_usage_created on tarik_model_usage (created_at);
-- The run it was made in (orchestrator.runs; the Executions and Discussions pages total them) and, for the agent's
-- calls, its prompt (tarik_prompt_calls; the LLM Calls pages).
alter table tarik_model_usage add column if not exists run_id uuid;
alter table tarik_model_usage add column if not exists prompt_call integer;
create index if not exists tarik_model_usage_run on tarik_model_usage (run_id);
create index if not exists tarik_model_usage_prompt on tarik_model_usage (prompt_call);

-- The invoices a processing run is working on: another run (another conversation) leaves them alone until it ends
-- (run_summary releases them) or stops refreshing its claim for 30 minutes (a run cut short).
create table if not exists tarik_working (
    invoice_id integer primary key,
    run_id text not null,
    claimed_at timestamptz not null default now()
);

-- The suppliers the client said have no attestation de regularite fiscale: not asked again for one (Experio then
-- applies its rate for a supplier without one). Sending one later clears it.
create table if not exists tarik_no_certificate (
    partner_id integer primary key,
    said_at timestamptz not null default now()
);

-- The cabinet's routines: a task Tarik runs again and again on a schedule (started by engine/routines.py, run by
-- engine/routine.py), made by a manager
-- (engine/tools/dossiers/routines.py). Kept in the cabinet's own schema: a routine never runs for another cabinet.
create table if not exists tarik_routines (
    id serial primary key,
    kind text not null check (kind in ('tva', 'ras', 'progress')), -- prepare the TVA or RAS, or report progress
    every text not null check (every in ('day', 'week', 'month')),
    days integer[] not null default '{}', -- weekdays 0-6 (Monday 0) each week, or the one day 1-28 each month
    hour integer not null check (hour between 0 and 23), -- Africa/Casablanca time
    notify text not null, -- who gets each run's report (an address the cabinet whitelists)
    created_by text not null, -- who made it: the email's sender, or 'chat' (who writes there is not known)
    enabled boolean not null default true,
    next_run_at timestamptz not null,
    created_at timestamptz not null default now()
);
-- An instruction routine: its own words, handed to Tarik's agent at each run. Its kind added to the allowed ones
-- (dropped and added again at each start, so it applies to schemas made before).
alter table tarik_routines add column if not exists prompt text not null default '';
-- A routine works on every dossier Tarik's Experio account sees: no list of its own any more. The routines made with
-- a list are paused once as the list goes, so none starts covering the whole cabinet before someone turns it on.
do $$ begin
    if exists (select from information_schema.columns
               where table_schema = current_schema() and table_name = 'tarik_routines' and column_name = 'dossier_ids')
    then
        update tarik_routines set enabled = false;
        alter table tarik_routines drop column dossier_ids;
    end if;
end $$;
-- A weekly routine on several weekdays: its days as a list (the month's one day too), in place of a single day.
alter table tarik_routines add column if not exists days integer[] not null default '{}';
do $$ begin
    if exists (select from information_schema.columns
               where table_schema = current_schema() and table_name = 'tarik_routines' and column_name = 'day')
    then
        update tarik_routines set days = case when every = 'day' then '{}' else array[day] end;
        alter table tarik_routines drop column day;
    end if;
end $$;
-- A routine's minute past its hour (0 at first: on the hour).
alter table tarik_routines add column if not exists minute integer not null default 0 check (minute between 0 and 59);
-- A routine's own name (« Rappel Manager »), its email's subject; '' for none.
alter table tarik_routines add column if not exists name text not null default '';
alter table tarik_routines drop constraint if exists tarik_routines_kind_check;
alter table tarik_routines add constraint tarik_routines_kind_check
    check (kind in ('tva', 'ras', 'progress', 'instruction'));
-- Each period a routine ran for: the same period never runs twice, after a restart included.
create table if not exists tarik_routine_runs (
    routine_id integer not null references tarik_routines (id) on delete cascade,
    due_at timestamptz not null,
    report text, -- what was sent, once the run ended
    ran_at timestamptz not null default now(),
    primary key (routine_id, due_at)
);

-- How Tarik behaves for this cabinet (engine/tools/dossiers/behaviour.py): a setting changed on the Comportement page
-- or in a conversation; one without a row has its default.
create table if not exists tarik_behaviour (
    key text primary key,
    value jsonb not null,
    changed_by text not null,
    changed_at timestamptz not null default now()
);
-- A thread's emails, read by thread at every claim and progress email.
create index if not exists tarik_mail_messages_thread on tarik_mail_messages (thread_id);
