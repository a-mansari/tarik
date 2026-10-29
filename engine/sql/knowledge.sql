-- name: knowledge_lock
-- One loader at a time (the server and a terminal command may start together), till the transaction ends: the
-- tables' making included, two "create table if not exists" at once can collide.
select pg_advisory_xact_lock(hashtext('tarik knowledge load'));

-- name: knowledge_schema
-- What Tarik may look up in the texts accountants follow (engine/knowledge/): public texts, the same for every
-- cabinet, in a schema of their own (knowledge), apart from the orchestrator's runs and accounts. Only the loader
-- writes them (engine/knowledge/cgnc.py load); a cabinet's connection (search_path its own schema) reads them by
-- their qualified names. Made at each start of the server (idempotent).
create schema if not exists knowledge;
-- The CGNC split in pieces (engine/knowledge/split_cgnc.py), word for word.
create table if not exists knowledge.cgnc_pieces (
    id integer primary key,  -- its order in the CGNC
    place text not null,  -- Titre › plan › Chapitre or Classe › its heading
    page_start integer not null,  -- the PDF's own pages
    page_end integer not null,
    codes text[] not null,  -- every account code it names, its heading's first: a code finds it, and its links
    body text not null,
    words tsvector not null  -- its place and body, accents left out (both sides: words.normalized), French stems
);
create index if not exists cgnc_pieces_words on knowledge.cgnc_pieces using gin (words);
create index if not exists cgnc_pieces_codes on knowledge.cgnc_pieces using gin (codes);
-- The CGNC's chart of accounts (modèle normal), Titre III.
create table if not exists knowledge.cgnc_accounts (
    code text primary key,
    label text not null,
    parent text  -- the account it is a part of (6125 for 61251), none for a rubric (61)
);
-- Which version of each text is loaded: a digest of its files and of how they are loaded, so a start reloads it only
-- when one of them changed.
create table if not exists knowledge.sources (
    name text primary key,
    digest text not null,
    loaded_at timestamptz not null default now()
);

-- name: knowledge_digest
select digest from knowledge.sources where name = %s;

-- name: knowledge_loaded
insert into knowledge.sources (name, digest) values (%s, %s)
on conflict (name) do update set digest = excluded.digest, loaded_at = now();

-- name: cgnc_forget
-- Delete, not truncate: a search meanwhile reads the old rows, never waits on the load.
delete from knowledge.cgnc_pieces;
delete from knowledge.cgnc_accounts;

-- name: cgnc_piece_saved
insert into knowledge.cgnc_pieces (id, place, page_start, page_end, codes, body, words)
values (%(id)s, %(place)s, %(page_start)s, %(page_end)s, %(codes)s, %(body)s,
        setweight(to_tsvector('french', %(place_words)s), 'A') || to_tsvector('french', %(body_words)s));

-- name: cgnc_account_saved
insert into knowledge.cgnc_accounts (code, label, parent) values (%(code)s, %(label)s, %(parent)s);

-- name: cgnc_found
-- The pieces for an account and words. With codes (the code asked, then the accounts it is part of): those naming
-- one; with words alone: those holding any (any_words, "a or b"). Words given, a piece holding all of them
-- (all_words) comes first. Then, with codes, the longest code first (61251's own before 6125's) and the piece naming
-- it soonest (the poste it heads before a piece that cites it); last, the closer together the words the better
-- (ts_rank_cd, its length aside: a long piece is no closer for its many words).
select place, page_start, page_end, codes, body
from knowledge.cgnc_pieces, websearch_to_tsquery('french', %(any_words)s) as any_of,
     websearch_to_tsquery('french', %(all_words)s) as every
where (cardinality(%(codes)s::text[]) > 0 and codes && %(codes)s::text[])
   or (cardinality(%(codes)s::text[]) = 0 and words @@ any_of)
order by words @@ every desc,
         (select max(length(code)) from unnest(codes) as code where code = any(%(codes)s::text[])) desc nulls last,
         (select min(at) from unnest(codes) with ordinality as named(code, at) where code = any(%(codes)s::text[])),
         ts_rank_cd(words, any_of, 1) desc, id
limit %(limit)s;

-- name: cgnc_accounts_named
-- The accounts among the codes given.
select code, label from knowledge.cgnc_accounts where code = any(%s);
