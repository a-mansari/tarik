-- The orchestrator's statements, on its own tables (schema orchestrator, the pool's search_path).

-- === Runs and steps (engine/runs.py) ===

-- name: dashboard_metrics
-- A cabinet's last days, for its dashboard: the runs started on their own (not those a step started), their
-- average time and the share that failed among those ended, and its conversations (Chat and email).
select count(*) filter (where parent_id is null),
    avg(extract(epoch from ended_at - started_at)) filter (where parent_id is null and ended_at is not null),
    count(*) filter (where parent_id is null and status = 'error')::float
        / nullif(count(*) filter (where parent_id is null and ended_at is not null), 0),
    count(distinct session_id) filter (where graph in ('discussion', 'fil'))
from runs where cabinet = %s and started_at > now() - %s * interval '1 day'

-- name: dashboard_usage
-- A cabinet's last days of model calls ({usage}: its tarik_model_usage), per model, the costliest first; priced:
-- the calls whose cost is known.
select model, sum(input_tokens), sum(cached_tokens), sum(output_tokens), sum(cost), count(cost) = count(*)
from {usage} where created_at > now() - %s * interval '1 day'
group by model order by sum(cost) desc nulls last

-- name: run_created
-- A new run, queued; parent_id and parent_node when started in another run's step.
insert into runs (id, graph, cabinet, session_id, thread_id, status, input, parent_id, parent_node)
values (%s, %s, %s, %s, %s, 'queued', %s, %s, %s)

-- name: run_running
update runs set status = 'running' where id = %s

-- name: step_recorded
-- One step as it ended: its subgraphs (path), its run of its part (lane), its round, what it said.
insert into steps (run_id, path, lane, round, node, status, summary, detail) values (%s, %s, %s, %s, %s, %s, %s, %s)

-- name: runs_cut_off
-- At the server's start: the runs a restart cut off (queued or running, kept in no queue) end in error.
update runs set status = 'error', error = 'Interrompu : le serveur a redemarre', ended_at = now()
where status in ('queued', 'running')

-- name: run_ended
update runs set status = %s, output = %s, error = %s, ended_at = now() where id = %s

-- name: run_found
select id, graph, cabinet, session_id, thread_id, status, input, output, error, started_at, ended_at, parent_id, parent_node
from runs where id = %s

-- name: run_children
-- The runs started in its steps, oldest first.
select id, graph, status, started_at, parent_node from runs where parent_id = %s order by started_at

-- name: run_graph
select graph from runs where id = %s

-- name: runs_recent
-- The latest runs, newest first: started on their own (no parent) unless a graph is asked for, then that graph's,
-- children too; a cabinet's only when one is asked for.
select id, graph, cabinet, session_id, status, error, started_at, ended_at, parent_id from runs
where (%(graph)s::text is not null or parent_id is null)
    and (%(cabinet)s::text is null or cabinet = %(cabinet)s)
    and (%(graph)s::text is null or graph = %(graph)s)
order by started_at desc limit %(limit)s

-- name: runs_under
-- The runs started in the steps of these runs, oldest first.
select id, graph, cabinet, session_id, status, error, started_at, ended_at, parent_id from runs
where parent_id = any(%s) order by started_at

-- name: steps_after
select id, at, path, lane, round, node, status, summary from steps where run_id = %s and id > %s order by id

-- name: run_state
select status, error from runs where id = %s

-- name: step_detail
select detail from steps where run_id = %s and id = %s

-- name: runs_cabinets
select distinct cabinet from runs order by 1

-- === Canvas layouts (server/layouts.py) ===

-- name: layout_read
select layout from layouts where graph = %s

-- name: layout_saved
insert into layouts (graph, layout) values (%s, %s)
on conflict (graph) do update set layout = excluded.layout, updated_at = now()

-- name: layout_forgotten
delete from layouts where graph = %s

-- === Accounts and sessions (server/accounts.py) ===

-- name: user_saved
-- An account made, or its name and password changed (and reactivated).
insert into users (email, name, password) values (%s, %s, %s)
on conflict (email) do update set name = excluded.name, password = excluded.password, active = true

-- name: user_role
-- An account's role changed: its open sessions carry the new one at their next page.
update users set role = %s where email = %s returning email

-- name: users_listed
-- Every account, for the Comptes page: whether it has a password yet, its last session.
select u.email, u.name, u.role, u.active, u.created_at, u.password is not null,
    (select max(s.expires_at) from sessions s where s.email = u.email)
from users u order by u.active desc, u.email

-- name: user_invited
-- A new account, no password yet: its person sets one through its invitation. Nothing for an email already known.
insert into users (email, name, password, role) values (%s, %s, null, %s) on conflict (email) do nothing returning email

-- name: user_found
select email from users where email = %s

-- name: admins_active
select email from users where role = 'admin' and active

-- name: invitations_removed
-- An account's links not used yet, gone: a new link replaces them, a closed account has none.
delete from invitations where email = %s and used_at is null

-- name: invitation_made
insert into invitations (token, email, expires_at) values (%s, %s, now() + %s * interval '1 hour')

-- name: invitation_found
-- The account a link is for, while it can still be used.
select i.email, u.name from invitations i join users u using (email)
where i.token = %s and i.expires_at > now() and i.used_at is null

-- name: invitation_used
-- A link used, once: a second use finds nothing.
update invitations set used_at = now() where token = %s and expires_at > now() and used_at is null returning email

-- name: user_password_set
-- The password its person chose, the account open again; its other sessions end (whoever had the old one).
update users set password = %s, active = true where email = %s

-- name: user_closed
update users set active = false where email = %s

-- name: user_sessions_ended
delete from sessions where email = %s

-- name: user_password
select password from users where email = %s and active

-- name: session_opened
insert into sessions (token, email, expires_at) values (%s, %s, now() + %s * interval '1 hour')

-- name: sessions_expired_removed
delete from sessions where expires_at < now()

-- name: session_user
-- Who opened a session still valid, their account still open.
select u.email, u.name, u.role from sessions s join users u using (email)
where s.token = %s and s.expires_at > now() and u.active

-- name: session_closed
delete from sessions where token = %s

-- === Content versions (engine/content.py) ===

-- name: content_latest
-- Each document's latest version; a document retired (its latest version empty) is no longer one.
select path, text from (select distinct on (path) path, text from content_versions order by path, id desc) latest
where text <> ''

-- name: content_saved
insert into content_versions (path, text, author, note) values (%s, %s, %s, %s)

-- name: content_history
select id, author, note, created_at from content_versions where path = %s order by id desc limit %s

-- name: content_version
-- A version's text, and the one before it.
select text from content_versions where path = %s and id <= %s order by id desc limit 2

-- name: content_last_changes
select distinct on (path) path, author, created_at from content_versions order by path, id desc

-- === Prompts (engine/prompts.py): in a cabinet's own schema, its tables named by the code ({calls}, {parts}) ===

-- name: prompt_saved
-- One model call: its distinct pieces kept once (by hash), the call as their hashes.
with new_parts as (
    insert into {parts} (hash, content) select hash, content::jsonb
    from unnest(%s::text[], %s::text[]) as t(hash, content) on conflict do nothing
)
insert into {calls} (session_id, agent, system_hash, tools_hash, message_hashes) values (%s, %s, %s, %s, %s)
returning id

-- name: prompt_calls_expired_removed
delete from {calls} where created_at < now() - %s * interval '1 day'

-- name: prompt_parts_unused_removed
-- A part younger than a day may belong to a call being saved right now.
delete from {parts} p where p.created_at < now() - interval '1 day'
    and not exists (
        select 1 from {calls} c
        where p.hash = c.system_hash or p.hash = c.tools_hash or p.hash = any(c.message_hashes)
    )

-- name: prompt_calls_of
-- A conversation's model calls, in order, optionally those of one run's time.
select id, agent, created_at, cardinality(message_hashes) from {calls} where session_id = %s
    and (%s::timestamptz is null or created_at >= %s)
    and (%s::timestamptz is null or created_at <= %s)
order by id

-- name: prompt_calls_listed
-- The cabinet's model calls, latest first (before an id, paging back), each with the agent run it was made in.
select c.id, c.agent, c.created_at, c.session_id, cardinality(c.message_hashes),
    (select r.id from {runs} r where r.graph = 'agent' and r.cabinet = %(cabinet)s and r.session_id = c.session_id
        and c.created_at >= r.started_at and c.created_at <= coalesce(r.ended_at, now())
     order by r.started_at desc limit 1)
from {calls} c where c.id < %(before)s order by c.id desc limit %(limit)s

-- name: usage_of_runs
-- Each run's model calls ({usage}: the cabinet's tarik_model_usage), the runs started in its steps included, at any
-- depth; same columns as dashboard_usage, the run's id first.
with recursive tree (root, id) as (
    select id, id from {runs} where id = any(%(keys)s::uuid[])
    union all
    select tree.root, r.id from {runs} r join tree on r.parent_id = tree.id
)
select tree.root::text, sum(u.input_tokens), sum(u.cached_tokens), sum(u.output_tokens), sum(u.cost),
    count(u.cost) = count(*)
from tree join {usage} u on u.run_id = tree.id group by tree.root

-- name: usage_of_sessions
-- Each conversation's model calls, every run of it; same columns as dashboard_usage, the conversation first.
select r.session_id, sum(u.input_tokens), sum(u.cached_tokens), sum(u.output_tokens), sum(u.cost),
    count(u.cost) = count(*)
from {usage} u join {runs} r on r.id = u.run_id
where r.cabinet = %(cabinet)s and r.session_id = any(%(keys)s::text[]) group by r.session_id

-- name: usage_of_calls
-- Each agent model call's tokens and cost, by its prompt's id; same columns as dashboard_usage, the call first.
select prompt_call, sum(input_tokens), sum(cached_tokens), sum(output_tokens), sum(cost), count(cost) = count(*)
from {usage} where prompt_call = any(%(keys)s::int[]) group by prompt_call

-- name: prompt_call
select agent, created_at, session_id, system_hash, tools_hash, message_hashes from {calls} where id = %s

-- name: prompt_parts_of
select hash, content from {parts} where hash = any(%s)

-- === The Discussion page (ui/chat_pages.py), in a cabinet's schema ===

-- name: conversations_first_messages
-- Each conversation's first message: as sent from the Discussions page, or an email thread's (with its subject and
-- sender), and whether a routine began the conversation (a Routine run in it: its instruction, or its report a reply
-- answers; runs_session's index).
select distinct on (r.session_id) r.session_id, coalesce(r.input ->> 'message', r.input -> 'thread' ->> 'message'),
    coalesce(r.input -> 'files', r.input -> 'thread' -> 'files'), r.input -> 'thread' ->> 'subject',
    r.input -> 'thread' ->> 'to',
    exists (select 1 from runs x where x.cabinet = r.cabinet and x.session_id = r.session_id and x.graph = 'routine')
from runs r
where r.graph in ('discussion', 'fil') and r.cabinet = %s and r.session_id = any(%s) order by r.session_id, r.started_at

-- name: sessions_waiting_for_client
-- The conversations whose client Tarik waits for, in a cabinet's schema: a question asked, or verified drafts to post
-- once told to.
select session_id from {pending} where session_id = any(%s)
union select session_id from {held} where session_id = any(%s) and not released

-- name: chat_file
-- A file a client sent in a conversation, as sent: the first saved under that name, never a part cut out of it.
select file_name, mime_type, data from {files} where session_id = %s and file_name = %s and parent_id is null
order by id limit 1

-- name: chat_outbox_file
-- A file Tarik made for a conversation (an export), the latest of that name.
select file_name, mime_type, data from {outbox} where session_id = %s and file_name = %s order by id desc limit 1

-- name: chat_outbox_names
-- The files Tarik made for a conversation, each name once, in the order made.
select file_name from {outbox} where session_id = %s group by file_name order by min(id)

-- === A cabinet's settings, changed from the Cabinets page (server/cabinet_settings.py) ===

-- name: settings_changed
insert into settings_changes (cabinet, author, note, changes, vault_version) values (%s, %s, %s, %s, %s)

-- name: model_adapter
select adapter from orchestrator.cabinet_models where cabinet = %s

-- name: model_adapter_set
insert into cabinet_models (cabinet, adapter) values (%s, %s)
on conflict (cabinet) do update set adapter = excluded.adapter, updated_at = now()

-- name: settings_history
select author, note, changes, vault_version, created_at from settings_changes where cabinet = %s
order by id desc limit %s

-- === Who did what in the UI (server/audit.py) ===

-- name: audit_recorded
insert into audit (actor, action, target, detail) values (%s, %s, %s, %s)

-- name: audit_listed
-- The latest entries before an id (paging back), of one action or one actor when asked.
select id, at, actor, action, target, detail from audit
where id < %(before)s and (%(action)s::text is null or action = %(action)s)
    and (%(actor)s::text is null or actor = %(actor)s)
order by id desc limit %(limit)s

-- name: audit_actors
select distinct actor from audit order by actor
