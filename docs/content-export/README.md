# Content exports

Prompt changes made on one stack, for the others. Prompts live in each stack's Postgres (`engine/content.py`), so a
change made on the Contenu page reaches no other stack by itself.

- `tarik-contenu-corrections-20261022.json`: the four documents changed on 2026-10-21/22 (`agent.json`,
  `skills/traitement-documents.md`, `skills/reponse-question.md`, `tools/classify-document.md`): reply sections,
  the classifier's « autre », `Experio_repondre`'s `compte` and `comptabiliser`, questions and retries kept to the
  conversation. Import it on the stack's Contenu page: each document becomes a new version, undoable there.
- `tarik-contenu-corrections-20261022.diff`: the same changes, before and after, to make them by hand on a stack whose
  own copies differ.

- `tarik-contenu-list-accounts-20261022.json` (and its `.diff`): `agent.json` and `skills/reponse-question.md` for
  `Experio_list_accounts` (commit 69b9dab): the tool's description, its line in the instructions' section A, and the
  skill's step that adds the close accounts to a repeated account question. Import it once the stack runs that code:
  before, the import is refused (it names a tool the stack lacks). It includes the 2026-10-22 corrections above.

`agent.json` is imported whole, its `model` (MiniMax-M3) included: set the stack's own model back afterwards if it
uses another one. A stack that edited one of these documents itself loses that edit on import: apply the diff instead.
