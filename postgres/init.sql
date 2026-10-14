-- Files clients send Tarik in the chat. n8n stores them; the MCP tools read them by id.
create table if not exists tarik_files (
    id serial primary key,
    session_id text not null,
    file_name text not null,
    mime_type text not null,
    data bytea not null,
    invoice_id integer, -- the draft invoice made from it: a second call must not make another
    created_at timestamptz not null default now()
);
