# Content exports

Prompt changes made on one stack, for the others. Prompts live in each stack's Postgres (`engine/content.py`), so a
change made on the Contenu page reaches no other stack by itself.

- `tarik-contenu-corrections-20261022.json`: the four documents changed on 2026-10-21/22 (`agent.json`,
  `skills/traitement-documents.md`, `skills/reponse-question.md`, `tools/classify-document.md`): reply sections,
  the classifier's « autre », `Experio_repondre`'s `compte` and `comptabiliser`, questions and retries kept to the
  conversation. Import it on the stack's Contenu page: each document becomes a new version, undoable there.
- `tarik-contenu-corrections-20261022.diff`: the same changes, before and after, to make them by hand on a stack whose
  own copies differ.

- `tarik-contenu-list-accounts-20261022.json` (and its `.diff`, against the export above): every document changed on
  2026-10-22 after that export (`agent.json`, `skills/reponse-question.md`, `skills/traitement-documents.md`,
  `tools/check-invoice.md`): `Experio_list_accounts` and its `side` (commits 69b9dab, 71dd2ab), the close accounts added
  to a repeated account question, `Experio_repondre`'s `corrections` [{facture, compte}] in place of `compte`, an
  account correction of an invoice already processed sent to `Experio_repondre`, and a payment line (Visa, CB) never
  taken for the designation. Import it once the stack runs commit 71dd2ab or later: before, the import is refused (it
  names a tool or a field the stack lacks).

- `tarik-contenu-w02-20261024.json` (and its `.diff`): W-02 (factures de vente), 2026-10-24 (`agent.json`,
  `labels.json`, `tools/check-invoice-answer.md`, `tools/check-invoice.md`): `Experio_solder_ecart` and its D ter
  instruction (écart de paiement), the canvas steps « Avertissements d'Experio » (W-01's `settle_warnings`) and « Écart
  de paiement », the purchase/sale wording of the steps that changed, and a hotel bill's restaurant, breakfast and
  laundry HT read apart. Import it once the stack runs the W-02 commit: before, `Experio_solder_ecart` is unknown.

- `tarik-contenu-w01-immobilisations-20261024.json` (and its `.diff`): `tools/choose-account.md` offers class 2 for a
  durable good (confirmed by the client before use), and `labels.json` says so for the account and settings steps.
  Import it after the W-02 export, once the stack runs the commit that confirms class 2 accounts.

- `tarik-contenu-w03-20261024.json` (and its `.diff`): W-03 (avoirs d'achat): the check reads the document's printed
  title and, on a credit note, the invoice it cancels (`tools/check-invoice*.md`); `labels.json` gets the step
  « Imputation de l'avoir ». Import it after the W-01 immobilisations export, once the stack runs the W-03 commit.

- `tarik-contenu-w05-20261024.json` (and its `.diff`): W-05 (dossiers d'import): the new prompt
  `tools/classify-import-page.md` (each page of an import folder typed among Experio's types) and the step « Dossier
  d'import » in `labels.json`. Import it after the W-03 export, once the stack runs the W-05 commit.

`agent.json` is imported whole, its `model` (MiniMax-M3) included: set the stack's own model back afterwards if it
uses another one. A stack that edited one of these documents itself loses that edit on import: apply the diff instead.
