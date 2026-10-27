# Research: pointage, progress updates, routines (2026-10-27)

Three proposals, each built on what Experio and Tarik already do. Experio paths are under
`../experio_app/addons/`. BSL = `bank/models/bank_statement_line.py`.

## 1. Pointage: matching bank operations with tiers and invoices

### What exists

**Experio's « Auto Point »** (`action_refresh` → `detect_bank_statement_line_info`, BSL:2339-2475).
- It copies the operation, payment method and tiers from the one past posted line whose label is most alike (trigram
  similarity > 0.8, BSL:2360-2409).
- It fills the tiers only when the dossier has `auto_detect_line_partner` on, which is off by default
  (`bank/models/res_company.py:29-34`).
- It then looks for an invoice only on the lines it just filled. The invoice must be of that tiers, owe exactly the
  line's amount, and be on no other line; the first such invoice wins (BSL:2439-2475).
- It never matches:
  - a partial payment;
  - one line paying several invoices, or several lines paying one;
  - fees or RAS differences;
  - a line without tiers;
  - labels below the similarity threshold.

**Tarik today** (`engine/tools/invoices/statement.py`).
- It runs Auto Point and confirms what it found.
- It ties a line to an invoice the client names by its number.
- It ties TPE remittances to their TPE statements.
- After posting, it lists the operations still in suspense.

**Experio's own tools for the rest.**
- **Suspense screen:** on a posted line, `invoice_ids` stays editable. Writing it letters the line at once
  (`reconcile_invoice_ids`, BSL:4293-4382). This is how Experio matches invoices that arrive after posting; nothing
  re-matches them automatically.
- **Split wizard** (`bank/wizard/bank_statement_split_wizard.py`): one line paying several invoices, and fees or RAS
  differences (tolerance 0.5).
- **Invoice outstanding-payments widget:** lines of the same tiers and account (`exp_account/models/exp_move.py:497-560`).

### Two faults in Tarik's `named_invoices`, found on the way

- **The tiers is left empty.** It writes `invoice_ids` without `partner_id`. An RPC write runs no onchange (the UI
  fills the tiers from the invoice, BSL:2138-2160), so a line without tiers stays draft with « tiers obligatoire ».
- **An invoice can be linked twice.** It does not apply Experio's `already_in_use` rule, so it can link an invoice
  already on another line. Experio then refuses the whole statement at « Comptabiliser »: `verify_selected_invoices`
  runs only from `action_post`, the onchange and the split wizard, never from a write.

### Proposal: Experio first, only sure matches applied

Put this after Auto Point in `settle_statement`. Every match goes through the same local check before it is written:
- the invoice's account fits the operation;
- the line's side fits the invoice (purchase or sale, invoice or refund);
- the sum rule;
- the invoice is not in draft;
- the invoice is not already on another line.

This mirrors `verify_selected_invoices` (BSL:4485-4596).

1. **Tiers for lines without one.** Look in the label for:
   - an ICE (15 digits), an IF, or a RIB (`bank_ids.acc_number`, when `entreprise_bank` is installed);
   - a tiers code, or the tiers' name as whole words.

   Use Experio's own keys (`exp.partner.get_existing_partner_map`, `exp_account/models/exp_partner.py:530-560`).
   Take it only when exactly one tiers fits, and that tiers fits the operation (supplier or customer).
2. **Invoices for purchase and sale lines that have a tiers.** Search with the line's own `invoice_ids_domain`
   (BSL:4236-4291), less the invoices already in use.
   - **Sure, applied:**
     - one open invoice whose remainder equals the amount to the centime; or
     - a number named in the label or by the client, whose remainder is no more than the amount.

     Write `{"partner_id": ..., "invoice_ids": [(4, id)]}` together.
   - **Probable, asked:**
     - several invoices of the tiers that add up exactly to the amount (at most 5 among the 30 latest, dated before
       the line); or
     - a difference that matches the tiers' RAS rate or a bank fee.

     The question lists the invoices; the split wizard's layout is what the cabinet then applies.
   - **Ties:** never applied, always asked.
3. **After posting, again with each new invoice.** Suspense lines are posted purchase or sale lines, unpaid or
   partly paid, and not reconciled. Match each one against the open invoices of its tiers, using the line's
   `amount_residual`. A sure match is written on the posted line, as the Suspense screen does, so Experio letters it.
   Read back `payment_state` and `is_reconciled` to confirm.

   This runs when a new invoice is posted, and as a weekly routine (section 3).

**Risk:** on a posted line, writing `invoice_ids` letters at once, so a wrong match changes the books immediately.
That is why only « sure » matches are written there; the rest are asked.

**To prove before building:**
- the sign of `amount_residual` on credit notes;
- the waiting accounts 3497/4497 on a real dossier;
- `_reconcile_plan` in `exp_account`.

Test both on the test replica, cabinet `experio_cabinet_8`.

## 2. Keeping the client informed without spamming

### Today

- One acknowledgement per thread, then one reply when the run ends (`engine/emails.py`). Follow-ups come at most
  every 30 minutes.
- The agent has no email tool, so nothing is sent mid-run.
- The manager alert is limited to once a day per sender, through `tarik_mail_alerts`.
- Nothing tracks what the client has already been told.

### Proposal: one notice queue, three speeds

Add a per-cabinet table `tarik_notices`, filled by `record_step` from the outcomes it already writes to
`tarik_results`. Its columns: session, dossier, kind, text, created_at, sent_at, channel.

A sender, on the mail tick, flushes the queue by kind:

| Kind | Example | When it goes out |
|---|---|---|
| question | « quel compte ? », « de quelle banque ? » | at once, all questions of the thread in one email |
| done | invoices posted, statement imported | in the run's final reply, as today |
| progress | « Experio lit encore 12 pièces » | never alone: added to the next email, or to the digest |
| routine report | monthly progress, suspense matched | in the digest |

Rules that keep it quiet:
- **At most one email per thread every N minutes** (setting, default 30), except a question the run waits on.
- **One reply per thread**, in-thread (In-Reply-To, already done), so one conversation per subject.
- **A daily digest per person** for routine and progress items. It goes out at a set hour, only when there is
  something new. It reuses the once-a-day pattern of `tarik_mail_alerts`.
- **A status link in each email** (« Suivre l'avancement ») to a read-only page of that conversation's outcomes and
  the dossier's progress (item 4's `dossier_progress`), signed per session. The client looks when they want: no push
  needed.

**Future channels** (WhatsApp, Slack): the queue holds kind and text, and each channel has its own sender with the
same rules. Only the sender changes.

## 3. Routines: scheduled, repeating tasks

### Today

- There is no scheduler. The only timers are the mail watcher's 15-minute tick and IDLE (`engine/mail.py`).
- LangGraph's open-source parts have no cron (crons are a LangGraph Platform feature), so it has to be ours.

### Proposal: a routine table and one loop

A per-cabinet table `tarik_routines`, so a routine never leaves its cabinet. Its columns:

- name and description;
- `workflow` (traitement, dossier, declarations, export, or a read such as `dossier_progress`) and `inputs` (jsonb);
- `dossiers`: one, a list, or all of the cabinet's;
- the schedule: `every` (day, week or month), `at` (hour), `day` (weekday or day of month), timezone;
- `rule`: a condition checked first, such as « only dossiers in monthly TVA mode » or « only when suspense > 0 »;
- `enabled`, `next_run_at`, `last_run_id`, `created_by`, `notify` (digest or at once).

**The loop.**
- One more task in the server, next to the mail watcher, wakes every minute.
- Per cabinet it runs `select … where enabled and next_run_at <= now() for update skip locked`.
- It starts the run with `runs.start` inside that cabinet, then sets the next date.
- A run is keyed by (routine, period), so a restart never runs a period twice.
- A missed period runs once at startup, not once for each time it was missed.
- Day, week and month cover the accounting needs, so there is no cron syntax and no new dependency.

**Creating one.**
- From the chat or by email, a manager asks: « chaque 5 du mois, prépare la TVA de mes dossiers mensuels ». A new
  agent tool, `Experio_routine` (create, list, pause, delete), behind `check_manager`, reads the routine back and
  asks for a yes before saving, like the other write tools.
- The UI gets a Routines page listing them, with their last runs (Executions).

**Safety.** A routine prepares, checks and reports; it never declares or posts what the client must confirm.
- The TVA routine prepares the declaration and asks.
- Test mode (`DRY_RUN`) applies as usual.
- Results go through the notice queue (section 2), usually the digest.

**First routines worth having:**
- monthly TVA or RAS preparation for the dossiers in that mode, on the day the cabinet sets;
- weekly suspense re-matching (section 1, step 3);
- a monthly dossier progress report to each client (`dossier_progress`);
- a reminder of missing bank statements: no statement for last month in a bank journal.
