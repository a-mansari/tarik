-- name: knowledge_schema
-- What Tarik may look up in the texts accountants follow (engine/knowledge/): the same for every cabinet, read
-- only, in schema orchestrator. Every name is qualified: a cabinet's connection (search_path its own schema) reads it
-- too. Made at each start of the server (idempotent).
create schema if not exists orchestrator;
-- The CGNC split in pieces (engine/knowledge/split_cgnc.py), word for word.
create table if not exists orchestrator.cgnc_pieces (
    id integer primary key,  -- its order in the CGNC
    place text not null,  -- Titre › Chapitre or Classe › its heading
    page_start integer not null,  -- the PDF's own pages
    page_end integer not null,
    codes text[] not null,  -- every account code it names: a code finds it, and its links to other accounts
    body text not null,
    words tsvector not null  -- its place and body, accents left out (both sides: words.normalized), French stems
);
create index if not exists cgnc_pieces_words on orchestrator.cgnc_pieces using gin (words);
create index if not exists cgnc_pieces_codes on orchestrator.cgnc_pieces using gin (codes);
-- The CGNC's chart of accounts (modèle normal), Titre III.
create table if not exists orchestrator.cgnc_accounts (
    code text primary key,
    label text not null,
    parent text  -- the account it is a part of (6125 for 61251), none for a rubric (61)
);
-- Which version of each text is loaded: a digest of its files, so a start reloads it only when they changed.
create table if not exists orchestrator.knowledge_sources (
    name text primary key,
    digest text not null,
    loaded_at timestamptz not null default now()
);

-- name: knowledge_lock
-- One loader at a time (the server and a terminal command may start together), till the transaction ends.
select pg_advisory_xact_lock(hashtext('tarik knowledge load'));

-- name: knowledge_digest
select digest from orchestrator.knowledge_sources where name = %s;

-- name: knowledge_loaded
insert into orchestrator.knowledge_sources (name, digest) values (%s, %s)
on conflict (name) do update set digest = excluded.digest, loaded_at = now();

-- name: cgnc_forget
truncate orchestrator.cgnc_pieces, orchestrator.cgnc_accounts;

-- name: cgnc_piece_saved
insert into orchestrator.cgnc_pieces (id, place, page_start, page_end, codes, body, words)
values (%(id)s, %(place)s, %(page_start)s, %(page_end)s, %(codes)s, %(body)s,
        setweight(to_tsvector('french', %(place_words)s), 'A') || to_tsvector('french', %(body_words)s));

-- name: cgnc_account_saved
insert into orchestrator.cgnc_accounts (code, label, parent) values (%(code)s, %(label)s, %(parent)s);

-- name: cgnc_found
-- The pieces for an account and words. With codes (the code asked, then the accounts it is part of): those naming
-- one, the longest code first (61251's own before 6125's), then those naming it soonest (the poste it heads before a
-- piece that cites it). With words alone: those holding any (any_words, "a or b"). Either way, a piece holding all
-- of them (all_words) comes before one holding some, then the closer together the better (ts_rank_cd, its length
-- aside: a long piece is no closer for its many words).
select id, place, page_start, page_end, codes, body
from orchestrator.cgnc_pieces, websearch_to_tsquery('french', %(any_words)s) as any_of,
     websearch_to_tsquery('french', %(all_words)s) as every
where case when cardinality(%(codes)s::text[]) > 0 then codes && %(codes)s::text[] else words @@ any_of end
order by (select max(length(code)) from unnest(codes) as code where code = any(%(codes)s::text[])) desc nulls last,
         (select min(at) from unnest(codes) with ordinality as named(code, at) where code = any(%(codes)s::text[])),
         words @@ every desc, ts_rank_cd(words, any_of, 1) desc, id
limit %(limit)s;

-- name: cgnc_accounts_named
-- The accounts among the codes given, in the plan's order.
select code, label from orchestrator.cgnc_accounts where code = any(%s) order by code;
