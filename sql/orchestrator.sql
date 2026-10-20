-- The orchestrator's statements, on its own tables (schema orchestrator, the pool's search_path).

-- === Runs and steps (engine/runs.py) ===

-- name: run_created
-- A new run, queued; parent_id and parent_node when started in another run's step.
insert into runs (id, graph, cabinet, session_id, thread_id, status, input, parent_id, parent_node)
values (%s, %s, %s, %s, %s, 'queued', %s, %s, %s)

-- name: run_running
update runs set status = 'running' where id = %s

-- name: step_recorded
-- One step as it ended: its subgraphs (path), its run of its part (lane), its round, what it said.
insert into steps (run_id, path, lane, round, node, status, summary, detail) values (%s, %s, %s, %s, %s, %s, %s, %s)

-- name: run_ended
update runs set status = %s, output = %s, error = %s, ended_at = now() where id = %s

-- name: run_found
select id, graph, cabinet, session_id, thread_id, status, input, output, error, started_at, ended_at, parent_id, parent_node
from runs where id = %s

-- name: run_steps
select id, at, path, lane, round, node, status, summary from steps where run_id = %s order by id

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

-- === Canvas layouts (engine/layouts.py) ===

-- name: layout_read
select layout from layouts where graph = %s

-- name: layout_saved
insert into layouts (graph, layout) values (%s, %s)
on conflict (graph) do update set layout = excluded.layout, updated_at = now()

-- name: layout_forgotten
delete from layouts where graph = %s

-- === Accounts and sessions (engine/accounts.py) ===

-- name: user_saved
-- An account made, or its name and password changed (and reactivated).
insert into users (email, name, password) values (%s, %s, %s)
on conflict (email) do update set name = excluded.name, password = excluded.password, active = true

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
select u.email, u.name from sessions s join users u using (email)
where s.token = %s and s.expires_at > now() and u.active

-- name: session_closed
delete from sessions where token = %s

-- === Content versions (engine/content.py) ===

-- name: content_latest
-- Each document's latest version.
select distinct on (path) path, text from content_versions order by path, id desc

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

-- name: prompt_calls_expired_removed
delete from {calls} where created_at < now() - %s * interval '1 day'

-- name: prompt_parts_unused_removed
-- A part younger than a day may belong to a call being saved right now.
delete from {parts} p where p.created_at < now() - interval '1 day'
    and not exists (
        select 1 from {calls} c
        where p.hash = c.system_hash or p.hash = c.tools_hash or p.hash = any(c.message_hashes)
    )

-- name: prompt_conversations
select session_id, count(*), array_agg(distinct agent), max(created_at) from {calls}
group by session_id order by 4 desc limit %s

-- name: prompt_calls_of
-- A conversation's model calls, in order, optionally those of one run's time.
select id, agent, created_at, cardinality(message_hashes) from {calls} where session_id = %s
    and (%s::timestamptz is null or created_at >= %s)
    and (%s::timestamptz is null or created_at <= %s)
order by id

-- name: prompt_call
select agent, created_at, session_id, system_hash, tools_hash, message_hashes from {calls} where id = %s

-- name: prompt_parts_of
select hash, content from {parts} where hash = any(%s)

-- === The Discussion page (ui/chat_pages.py), in a cabinet's schema ===

-- name: chat_file
-- A file a client sent in a conversation, as sent: the first saved under that name, never a part cut out of it.
select file_name, mime_type, data from {files} where session_id = %s and file_name = %s and parent_id is null
order by id limit 1
