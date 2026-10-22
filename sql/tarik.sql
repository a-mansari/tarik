-- name: create_cabinet_schema
-- === tarik/core/cabinets.py ===
-- A cabinet's own schema (its name filled in by the code), made if missing before its tables (cabinet_schema).
create schema if not exists {name}

-- name: last_client_message
-- === tarik/core/conversation.py ===
-- The conversation's latest client message: the same message twice in a row is kept once.
select message from tarik_client_messages where session_id = %s order by id desc limit 1

-- name: insert_client_message
-- Keep one client message of the conversation, word for word.
insert into tarik_client_messages (session_id, message) values (%s, %s)

-- name: client_messages_of_session
-- The conversation's client messages, oldest first.
select message from tarik_client_messages where session_id = %s order by id

-- name: kept_answer_of_step
-- The client's answer kept for one step's question on an invoice.
select answer from tarik_answers where invoice_id = %s and step = %s

-- name: upsert_answer
-- Keep the client's answer to one step's question on an invoice, replacing an earlier one.
insert into tarik_answers (invoice_id, step, answer) values (%s, %s, %s)
on conflict (invoice_id, step) do update set answer = excluded.answer

-- name: classification_of_invoice
-- === tarik/invoices/classify.py ===
-- The category and reason already decided for an invoice.
select category, reason from tarik_classifications where invoice_id = %s

-- name: upsert_classification
-- Store an invoice's category and its reason, replacing any earlier one.
insert into tarik_classifications (invoice_id, category, reason) values (%s, %s, %s)
on conflict (invoice_id) do update set category = excluded.category, reason = excluded.reason,
classified_at = now()

-- name: stored_check_of_invoice
-- === tarik/invoices/check.py ===
-- An invoice's check as stored: readable, extracted, bad_fields.
select readable, extracted, bad_fields from tarik_checks where invoice_id = %s

-- name: upsert_check
-- Store an invoice's check, replacing any earlier one.
insert into tarik_checks (invoice_id, readable, extracted, bad_fields) values (%s, %s, %s, %s)
on conflict (invoice_id) do update set readable = excluded.readable, extracted = excluded.extracted,
bad_fields = excluded.bad_fields, checked_at = now()

-- name: file_locked
-- === tarik/invoices/intake.py ===
-- A file as a tool reads it, its row locked until the transaction commits: a retry can't create a second draft nor
-- import a statement twice.
select session_id, file_name, mime_type, data, kind, invoice_id, statement_id, parent_id from tarik_files
where id = %s for update

-- name: set_file_kind
-- What identify_document found a file to be: invoice or statement.
update tarik_files set kind = %s where id = %s

-- name: insert_cut_invoice_file
-- One invoice cut out of a multi-invoice PDF, saved as its own file.
insert into tarik_files (session_id, file_name, mime_type, data, parent_id, kind)
values (%s, %s, %s, %s, %s, 'invoice') returning id

-- name: cut_parts_of_file
-- The invoices cut out of a file (id, file name), in order.
select id, file_name from tarik_files where parent_id = %s order by id

-- name: set_file_invoice
-- The draft invoice made from a file: a second call must not make another.
update tarik_files set invoice_id = %s where id = %s

-- name: lock_file_content
-- Held until the transaction ends: one import at a time of the same file's content.
select pg_advisory_xact_lock(hashtext(md5(%s)))

-- name: statement_of_same_content
-- The bank statement already imported into the dossier from a file of the very same content, another file.
select f.statement_id from tarik_files f join tarik_results r on r.file_id = f.id
where f.statement_id is not null and f.content_md5 = md5(%s) and r.step = 'import_bank_statement'
and r.status = 'ok' and r.dossier_id = %s and f.id <> %s order by f.id limit 1

-- name: set_file_statement
-- The bank statement imported from a file: a second call must not import it again.
update tarik_files set statement_id = %s where id = %s

-- name: last_question_of_session
-- === tarik/dossiers/identity.py ===
-- Tarik's last question in the conversation.
select message from tarik_results where session_id = %s and status = 'question' order by id desc limit 1

-- name: dossiers_created_in_session
-- The dossiers this conversation already created.
select distinct dossier_id from tarik_results where session_id = %s and
step = 'create_dossier' and status = 'ok' and dossier_id is not null

-- name: sender_of_session
-- === tarik/dossiers/people.py ===
-- Who wrote, for an email conversation.
select client_address from tarik_mail_threads where session_id = %s

-- name: file_taken_as_entries
-- The file is done with, imported or refused: a corrected file comes as a new one.
update tarik_files set kind = 'entries' where id = %s

-- name: upsert_setup_wait
-- === tarik/dossiers/setup.py ===
-- The dossier waits for its onboarding, in the conversation of the file whose import went through.
insert into tarik_setups (dossier_id, session_id) select %s, session_id from tarik_files
where id = %s on conflict (dossier_id) do update set session_id = excluded.session_id

-- name: delete_setup_wait
-- The dossier no longer waits for its onboarding.
delete from tarik_setups where dossier_id = %s

-- name: setups_waiting_in_session
-- The conversation's dossiers whose onboarding still waits, oldest first.
select dossier_id from tarik_setups where session_id = %s order by created_at

-- name: upsert_held_choice
-- === tarik/invoices/pipeline.py ===
-- The client's choice to post an invoice (released) or keep it a draft, kept per invoice.
insert into tarik_held (invoice_id, session_id, released) values (%s, %s, %s)
on conflict (invoice_id) do update set released = excluded.released

-- name: insert_held
-- An invoice held at its verified draft, unless a choice is already kept for it.
insert into tarik_held (invoice_id, session_id) values (%s, %s) on conflict do nothing

-- name: held_released
-- Whether the client asked to post a held invoice.
select released from tarik_held where invoice_id = %s

-- name: insert_result
-- One step's outcome for a run.
insert into tarik_results (run_id, session_id, file_id, invoice_id, dossier_id, step, status, message,
routine, choices, journal_id) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)

-- name: upsert_pending
-- An invoice's pending question, replacing an earlier one.
insert into tarik_pending (invoice_id, session_id, step, question, choices)
values (%s, %s, %s, %s, %s) on conflict (invoice_id) do update set step = excluded.step,
session_id = excluded.session_id, question = excluded.question, choices = excluded.choices,
created_at = now()

-- name: pending_step
-- The step an invoice's pending question waits at.
select step from tarik_pending where invoice_id = %s

-- name: delete_pending
-- An invoice's pending question, settled.
delete from tarik_pending where invoice_id = %s

-- name: run_outcomes
-- A run's outcomes, keyed by invoice (or minus the file id), in the order recorded.
select coalesce(invoice_id, -file_id), step, status, message, routine from tarik_results
where run_id = %s order by id

-- name: statements_asked
-- Each bank statement of a conversation not imported yet whose last import asked for its bank account.
select file_id, dossier_id, message, file_name, choices from (select distinct on (r.file_id) r.file_id,
r.dossier_id, r.status, r.message, r.choices, f.file_name from tarik_results r join tarik_files f
on f.id = r.file_id
where r.session_id = %s and r.step = 'import_bank_statement' and f.statement_id is null and f.kind = 'statement'
order by r.file_id, r.id desc) latest where status = 'question'

-- name: banks_asked
-- Each dossier of a conversation whose last bank question (ask_banks) is still a question.
select dossier_id, journal_id, message, choices from (select distinct on (dossier_id) dossier_id, journal_id,
status, message, choices from tarik_results where session_id = %s and step = 'ask_banks'
order by dossier_id, id desc) latest where status = 'question'

-- name: pending_of_session
-- The conversation's pending invoice questions, oldest first.
select invoice_id, step, question, choices from tarik_pending where session_id = %s order by created_at

-- name: delete_pending_of_invoices
-- The pending questions of invoices deleted or posted since: nothing can answer them any more.
delete from tarik_pending where invoice_id = any(%s)

-- name: new_files_of_session
-- The files that came since the conversation's last run recorded anything: not yet taken by any step, not cut
-- parts, not a dossier's entries (those go to the dossier workflow).
-- Not files_to_process, on purpose: what came since the last run, not all that is left.
select file_name from tarik_files where session_id = %s and invoice_id is null and statement_id is null
and parent_id is null and kind is distinct from 'entries' and created_at >
coalesce((select max(created_at) from tarik_results where session_id = %s), '-infinity')

-- name: pdfs_awaiting_split
-- The conversation's PDFs left uncut and without a draft: a document of several pages waits for the client to say
-- how to cut it (split_invoices asks), and their answer must reach it, whatever else waits.
select count(*) from tarik_files f where session_id = %s and kind = 'invoice' and invoice_id is null
and parent_id is null and mime_type = 'application/pdf'
and not exists (select 1 from tarik_files part where part.parent_id = f.id)

-- name: insert_agent_file
-- One of the agent's attachments, saved once per attachment id however many times the sequence runs.
insert into tarik_files (session_id, file_name, mime_type, data, source_id)
values (%s, %s, %s, %s, %s) on conflict (session_id, source_id) do nothing returning id

-- name: files_to_process
-- Whatever has no draft or statement yet, until it has: a file left uncut (its side wasn't given), one invoice
-- cut out of a PDF whose draft failed, a statement waiting for its bank account. A cut PDF is its parts.
select id, file_name from tarik_files f where session_id = %s and invoice_id is null
and statement_id is null and kind is distinct from 'entries'
and not exists (select 1 from tarik_files part where part.parent_id = f.id) order by id

-- name: invoice_outcomes_of_session
-- The conversation's invoice outcomes, in the order recorded.
select invoice_id, step, status from tarik_results where session_id = %s and invoice_id is not null
order by id

-- name: drafts_of_session
-- The drafts made from the conversation's files, in order.
select invoice_id from tarik_files where session_id = %s and invoice_id is not null order by id

-- name: outcomes_of_stopped_invoices
-- Every outcome of each invoice a step stopped, from any conversation, in the order recorded.
select invoice_id, step, status from tarik_results where invoice_id in
(select invoice_id from tarik_results where status = 'stop' and invoice_id is not null) order by id

-- name: held_not_released
-- The drafts kept unposted (invoice, conversation).
select invoice_id, session_id from tarik_held where not released

-- name: outcomes_of_posted_invoices
-- Every outcome of each invoice Tarik posted, from any conversation, in the order recorded.
select invoice_id, step, status from tarik_results where invoice_id in
(select invoice_id from tarik_results where step = 'post_invoice' and status = 'ok') order by id

-- name: invoices_made_by_tarik
-- Which of the invoices Tarik made from a file.
select invoice_id from tarik_files where invoice_id = any(%s)

-- name: pending_of_invoices
-- The pending question of each of the invoices.
select invoice_id, question from tarik_pending where invoice_id = any(%s)

-- name: outcomes_of_invoices
-- Every outcome of the invoices, with its message, in the order recorded.
select invoice_id, step, status, message from tarik_results where invoice_id = any(%s) order by id

-- name: thread_of_linked_message
-- === tarik/emails/inbox.py ===
-- The sender's thread holding one of the linked message ids (In-Reply-To, References): one of their emails, or one
-- Tarik sent in it.
select t.thread_id from tarik_mail_threads t where t.client_address = %(sender)s and t.thread_id in
(select thread_id from tarik_mail_messages where message_id = any(%(linked)s)
 union select thread_id from tarik_mail_sent where message_id = any(%(linked)s)) limit 1

-- name: insert_mail_sent
-- An email Tarik sent in a thread.
insert into tarik_mail_sent (message_id, thread_id) values (%s, %s) on conflict do nothing

-- name: insert_mail_thread
-- A new thread, new or ignored.
insert into tarik_mail_threads (thread_id, session_id, client_address, subject, state, reason)
values (%s, %s, %s, %s, %s, %s)

-- name: reopen_mail_thread
-- A thread the client wrote again in, back to Tarik, even after an error or a refusal (a run on it takes it after).
update tarik_mail_threads set state = 'new', attempts = 0, reason = null, follow_up_until = null,
updated_at = now()
where thread_id = %s and state <> 'in_progress'

-- name: mail_message_exists
-- Whether an email is already stored.
select 1 from tarik_mail_messages where message_id = %s

-- name: session_of_thread
-- A thread's conversation.
select session_id from tarik_mail_threads where thread_id = %s

-- name: insert_mail_message
-- One email, in its sender's thread.
insert into tarik_mail_messages (message_id, thread_id, uid, from_address, reply_to, subject, body,
references_ids, handled, skipped) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)

-- name: insert_mail_file
-- One file an email brought, in its thread's conversation.
insert into tarik_files (session_id, file_name, mime_type, data) values (%s, %s, %s, %s)

-- name: unhandled_messages_of_thread
-- A thread's unanswered emails, oldest first, and what their zips left out.
select message_id, body, skipped from tarik_mail_messages where thread_id = %s and not handled
order by received_at, uid

-- name: last_message_of_thread
-- A thread's last email: what the reply answers.
select message_id, references_ids, reply_to from tarik_mail_messages where thread_id = %s
order by received_at desc, uid desc limit 1

-- name: untaken_file_names
-- The conversation's files no tool has taken yet, in order. Unlike files_to_process (on purpose): a file
-- identify_document has given a kind is taken, even one still to cut.
select file_name from tarik_files where session_id = %s and parent_id is null and kind is null
and invoice_id is null and statement_id is null order by id

-- name: set_aside_failed_threads
-- Threads whose runs failed too many times (their lease over), set aside in error.
update tarik_mail_threads set state = 'error', reason = %s, claimed_at = null
where state = 'in_progress' and attempts >= %s and claimed_at < now() - make_interval(mins => %s)

-- name: claim_mail_threads
-- Lock for this run each thread Tarik must answer: new, or taken by a run that died (its lease of LEASE_MINUTES
-- is over), and each follow-up thread that is ready.
update tarik_mail_threads set state = 'in_progress', claimed_at = now(), attempts = attempts + 1,
updated_at = now() where state = 'new' or (state = 'in_progress' and claimed_at < now() - make_interval(mins => %s))
or (state = 'follow_up' and thread_id = any(%s))
returning thread_id, session_id, client_address, subject

-- name: follow_up_threads
-- The follow-up threads, and whether their time is up.
select thread_id, session_id, follow_up_until < now() from tarik_mail_threads where state = 'follow_up'

-- name: finish_follow_up
-- A follow-up thread with nothing left: done.
update tarik_mail_threads set state = 'done', follow_up_until = null, updated_at = now()
where thread_id = %s and state = 'follow_up'

-- name: mail_folder_state
-- Where the reading of a folder stands.
select uidvalidity, last_uid from tarik_mail_state where folder = %s

-- name: upsert_mail_folder_state
-- Where the reading of a folder starts: its newest uid, on a first reading or a folder rebuilt.
insert into tarik_mail_state (folder, uidvalidity, last_uid) values (%s, %s, %s) on conflict (folder)
do update set uidvalidity = excluded.uidvalidity, last_uid = excluded.last_uid

-- name: set_mail_folder_last_uid
-- The last email read in a folder.
update tarik_mail_state set last_uid = %s where folder = %s

-- name: try_mailbox_lock
-- A cabinet's reading lock, held until the connection closes.
select pg_try_advisory_lock(hashtext(%s))

-- name: any_thread_waiting
-- Whether a thread waits for Tarik: new, or taken by a run that died (its lease of LEASE_MINUTES is over).
select 1 from tarik_mail_threads
where state = 'new' or (state = 'in_progress' and claimed_at < now() - make_interval(mins => %s)) limit 1

-- name: set_aside_thread
-- Unlock a thread in a state ('ignored' or 'error'), with a reason.
update tarik_mail_threads set state = %s, reason = %s, claimed_at = null, updated_at = now()
where thread_id = %s

-- name: thread_to_acknowledge
-- A thread, and whether it is to be acknowledged: never acknowledged, and none of its emails answered.
select session_id, client_address, subject, acknowledged_at is null and not exists
(select 1 from tarik_mail_messages m where m.thread_id = t.thread_id and m.handled)
from tarik_mail_threads t where thread_id = %s

-- name: set_thread_acknowledged
-- The thread's acknowledgement was sent.
update tarik_mail_threads set acknowledged_at = now() where thread_id = %s

-- name: thread_of_refusal
-- A thread, and when this run claimed it.
select session_id, client_address, subject, claimed_at from tarik_mail_threads where thread_id = %s

-- name: sender_refused_since
-- The conversation's latest check_sender refusal since a moment.
select message from tarik_results where session_id = %s and step = 'check_sender' and status = 'stop'
and created_at >= %s order by id desc limit 1

-- name: handle_thread_messages
-- Every email of a thread, counted as handled.
update tarik_mail_messages set handled = true where thread_id = %s

-- name: manager_alerted_today
-- Whether the manager heard of a sender in the last day.
select 1 from tarik_mail_alerts where sender = %s and alerted_at > now() - interval '1 day'

-- name: insert_mail_alert
-- The manager heard of a sender.
insert into tarik_mail_alerts (sender) values (%s)

-- name: handle_mail_messages
-- The emails a reply answered, counted as handled.
update tarik_mail_messages set handled = true where message_id = any(%s)

-- name: thread_has_unhandled
-- Whether an email came in a thread that is not answered yet.
select 1 from tarik_mail_messages where thread_id = %s and not handled limit 1

-- name: session_has_pending
-- Whether the conversation has a question waiting for the client.
select 1 from tarik_pending where session_id = %s limit 1

-- name: follow_up_still_open
-- Whether a thread's follow-up time (FOLLOW_UP_MINUTES from its first follow-up) is not over.
select coalesce(follow_up_until, now() + make_interval(mins => %s)) > now()
from tarik_mail_threads where thread_id = %s

-- name: finish_mail_thread
-- A thread after Tarik's reply, in its new state; a follow-up keeps its first deadline.
update tarik_mail_threads set state = %s, claimed_at = null, attempts = 0, reason = null,
follow_up_until = case when %s = 'follow_up'
then coalesce(follow_up_until, now() + make_interval(mins => %s)) end,
updated_at = now() where thread_id = %s

-- name: claim_invoices
-- Of the invoices given, those this run may work on: free, already its own, or left by a run silent for 30 minutes.
insert into tarik_working (invoice_id, run_id) select unnest(%s::int[]), %s
on conflict (invoice_id) do update set run_id = excluded.run_id, claimed_at = now()
where tarik_working.run_id = excluded.run_id or tarik_working.claimed_at < now() - interval '30 minutes'
returning invoice_id

-- name: release_invoices
-- The run is over: its invoices are free.
delete from tarik_working where run_id = %s

-- name: invoices_worked_on
-- Of the invoices given, those a run is working on now (its claim refreshed in the last 30 minutes).
select invoice_id from tarik_working where invoice_id = any(%s) and claimed_at >= now() - interval '30 minutes'

-- name: other_file_names
-- The names of the other files of a file's conversation.
select file_name from tarik_files where session_id = (select session_id from tarik_files where id = %s) and id <> %s
