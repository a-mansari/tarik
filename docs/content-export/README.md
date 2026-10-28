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

- `tarik-contenu-w06-20261024.json` (and its `.diff`): W-06 (relevés bancaires): `comptabiliser` also posts an imported
  statement (`agent.json`), and the steps « Pointage du relevé » and « Comptabilisation du relevé » (`labels.json`).
  Import it after the W-05 export, once the stack runs the W-06 commit.
- `tarik-contenu-w07-20261025.json` (and its `.diff`): W-07 (relevés TPE): the client's answers to a TPE statement's
  network and its card sales' client (`agent.json`, Experio_repondre), and the TPE statement in the steps « Relevé »,
  « Pointage » and « Comptabilisation du relevé » (`labels.json`). Import it after the W-06 export, once the stack runs
  the W-07 commit.
- `tarik-contenu-w11-20261025.json` (and its `.diff`): W-11 (déclaration de TVA): the tool Experio_declaration_tva and
  its instruction (`agent.json`), the step « Déclaration de TVA » and the names of its inputs (`labels.json`). Import it
  after the W-07 export, once the stack runs the W-11 commit.
- `tarik-contenu-w12-20261025.json` (and its `.diff`): W-12 (déclarations RAS): the tool Experio_declaration_ras and its
  instruction (`agent.json`), the step « Déclarations RAS » and its input (`labels.json`). Import it after the W-11
  export, once the stack runs the W-12 commit.
- `tarik-contenu-w13-20261025.json` (and its `.diff`): W-13 (délais de paiement): the tool Experio_declaration_delais
  and its instruction (`agent.json`), the step « Délais de paiement » and its input (`labels.json`). Import it after
  the W-12 export, once the stack runs the W-13 commit.
- `tarik-contenu-w14-20261025.json` (and its `.diff`): W-14 (export): the tool Experio_exporter and its instruction
  (`agent.json`), the step « Export » and its inputs (`labels.json`). Import it after the W-13 export, once the stack
  runs the W-14 commit.
- `tarik-contenu-workflows-20261025.json` (and its `.diff`): the workflows « Déclarations » and « Export » apart from
  « Dossier », and the steps split one per node (Dossier: copie du paramétrage, fichiers, banques, fin du paramétrage,
  état; Traitement: dossier d'import; Fil d'emails: suite des documents, suite des dossiers) (`labels.json`). Import
  it after the W-14 export, once the stack runs the commit that splits them.
- `tarik-contenu-descriptions-20261026.json` (and its `.diff`): 50 step and workflow descriptions rewritten to say
  exactly what their code does (`labels.json`). Import it after the workflows export.
- `tarik-contenu-dossier-import-20261026.json` (and its `.diff`): « dossier d'import » with its pieces goes to
  Experio_traiter_fichiers, never to dossier creation (`agent.json`: instruction D 0 and both tools' descriptions).
  Import it after the descriptions export.
- `tarik-contenu-mise-en-forme-20261026.json` (and its `.diff`): emails easier to read at a glance: what Experio names
  (dossier, tiers, compte, journal...) written between backticks like file names (`agent.json`, rule 10), and an
  invoice's outcomes kept one per line under it, never merged (`skills/traitement-documents.md`,
  `skills/reponse-question.md`, step 2). Import it after the dossier-import export, once the stack runs the commit that
  lists them so.

`agent.json` is imported whole, its `model` (MiniMax-M3) included: set the stack's own model back afterwards if it
uses another one. A stack that edited one of these documents itself loses that edit on import: apply the diff instead.
- `tarik-contenu-acces-avancement-20261027.json` (and its `.diff`): the tools `Experio_gerer_acces` (give or take a
  dossier, change a profile, send the invitation again) and `Experio_dossier_progress` (where a dossier stands), the
  invitation and the managers-only rule in `Experio_creer_acces` (`agent.json`), the steps « Droit de gérer les accès »
  and « Accès modifié » with the input « Changement d'accès » (`labels.json`), and when to use them
  (`skills/gestion-dossiers.md`). Import it after the mise-en-forme export, once the stack runs the commit that adds
  them: before, the import is refused (it names tools the stack lacks).
- `tarik-contenu-routines-20261027.json` (and its `.diff`): the tool `Experio_routine` (create, list, pause, resume,
  delete a routine: prepare the TVA or the RAS, or send the progress, on a schedule) and when to use it
  (`agent.json`, `skills/gestion-dossiers.md`), the step « Routine » and its inputs (`labels.json`). Import it after the
  acces-avancement export, once the stack runs the routines commit: before, the import is refused (it names a tool
  the stack lacks).
- `tarik-contenu-routines-page-20261027.json` (and its `.diff`): the routine kind « instruction » and its `prompt` in
  `Experio_routine` (`agent.json`) and when to use it (`skills/gestion-dossiers.md`), the input « Consigne de la
  routine », the kind's name and the journal's « Routine modifiée » (`labels.json`). Import it after the routines
  export, once the stack runs the Routines page commit.
- `tarik-contenu-routines-sans-dossiers-20261027.json` (and its `.diff`): routines work on every dossier Tarik's
  Experio account sees: `Experio_routine` loses `dossier_ids` and says only admins and Experio's staff make one
  (`agent.json`), the skill no longer looks for dossiers first (`skills/gestion-dossiers.md`), the progress kind's
  name (`labels.json`). Import it after the routines-page export, once the stack runs the commit without the selector.
- `tarik-contenu-routines-jours-20261027.json` (and its `.diff`): a weekly routine on several weekdays: `Experio_routine`
  takes `days` (`agent.json`) and the input « Jours » (`labels.json`; « Jour » left for stacks still on the code before).
  Import it after the routines-sans-dossiers export, once the stack runs the commit with the weekdays.
- `tarik-contenu-routine-workflow-20261027.json` (and its `.diff`): the Routine workflow each routine starts at its
  time: its name, steps and their descriptions, and the inputs « Tâche de la routine »
  and « Période suivante » (`labels.json`). Import it after the routines-jours export, once the stack runs the commit
  with the Routine workflow: before, check.py finds no gap but the labels name a workflow the stack lacks.
- `tarik-contenu-routine-fichiers-20261027.json` (and its `.diff`): the Routine workflow's « Rapport » and
  « Rapport envoyé » steps say the report is a few lines in the email and every dossier in the files joined (Excel,
  CSV, text) (`labels.json`). Import it after the routine-workflow export, once the stack runs the commit with the
  report's files.
- `tarik-contenu-routine-outil-20261028.json` (and its `.diff`): `Experio_routine` changes a routine too (action
  `update`: only what the client changes) and takes `minute` and `routine_name` (`agent.json`), when to use it
  (`skills/gestion-dossiers.md`), the inputs « Minute » and « Nom de la routine » and the action « Modifier »
  (`labels.json`). Import it after the routine-fichiers export, once the stack runs the commit with the update action.
- `tarik-contenu-pointage-20261028.json` (and its `.diff`): pointing a dossier's statements already in Experio: the
  tool `Experio_pointer_releves` and when to use it (D octies, `agent.json`), the workflow « Relevés » and its steps,
  and the pointing step's new completions in Traitement (`labels.json`), and the new prompt
  `tools/point-bank-lines.md` (the operation and payment method read in a bank label). Import it after the
  routine-outil export, once the stack runs the pointage commit: before, the import is refused (it names a tool the
  stack lacks).
- `tarik-contenu-pointage-etapes-20261028.json` (and its `.diff`): a statement's pointing in steps (`labels.json`):
  the boxes « Détection d'Experio », « Types d'opération », « Tiers des opérations », « Factures citées », « Factures
  ouvertes », « Versements TPE » and « Bilan du relevé », the Relevés boxes « Relevés à pointer » and « Pointage du
  relevé », and Traitement's « Lecture du relevé » then « Pointage des opérations ». Import it after the pointage
  export, once the stack runs the commit with the pointing steps.
- `tarik-contenu-pointage-tiers-20261028.json` (and its `.diff`): the new prompt `tools/point-line-tiers.md` (the tiers
  read in a bank label that names none in whole words), the « pas de TVA » / « sans TVA » answers read as « oui » to
  the non-recoverable TVA question (`skills/reponse-question.md`), and the descriptions of the steps « Types
  d'opération », « Tiers des opérations » and « Comptabilisation du relevé » (`labels.json`). Import it after
  the pointage-etapes export, once the stack runs the commit with the tiers reading: before, check.py and the
  consistency test find no `tools/point-line-tiers.md`, and the tiers reading is skipped (the lines stay for the cabinet).
