# Using Tarik

What Tarik can do for a cabinet, and how to write to it for the best results. Tarik answers in the
language you write in, in short sentences.

## Where to reach it

By email, at the cabinet's own Tarik mailbox:

- **Files accepted**: PDF, images, CSV, TXT, XLSX, any size the mail server accepts. A **ZIP** is opened and the files
  in it are processed as if sent one by one (a zip inside it too, not deeper; password-protected files, and types
  Tarik does not take, are left out, and the
  reply says which and why).
- **Who may use it**: only the cabinet's whitelisted addresses (`@experio.ma`, `@sarnec.com`, …), as the email's
  From says; for an existing dossier, a collaborator (or one of Experio's own staff) who has that dossier in Experio.
  A colleague replying in your thread gets their own answer.
- **When it answers**: within seconds of the email arriving (15 minutes at most).

Or, for the team, on the **Discussion** page of Tarik's pages (logged in): pick the cabinet, start a conversation,
write and attach files (same types as email, 10 per message, 20 MB each). Tarik answers on the page; each message
links to its run, step by step. No sender check there: whoever is logged in writes.

## What Tarik can do

**Answer questions**
- Whether Experio works.
- The cabinet's dossiers, or find one from a name, even misspelled or partial.
- A dossier's invoices, optionally only those being read, drafts, posted or paid.
- The cabinet's collaborators: role, manager or not, number of dossiers.

**Process invoices and bank statements.** An invoice is a purchase (received from a supplier) or a
sale (issued by the dossier to its own customer): say which, or Tarik asks before anything is made. For
each file: it recognises an invoice or a statement,
splits a PDF holding several invoices, creates each draft, checks what Experio read against the scan
and corrects it, then settles the tiers, account, TVA, journal, withholding tax, prepaid charges and
fiscal year, then each warning Experio gives (fixed, waited out or dismissed when it can't apply: see
docs/experio-warnings.md), and leaves it as a verified draft: it is posted only when you ask
("comptabilisez"), in the same message or later. When a step needs you (which account? which fiscal year?),
it asks. For a local purchase whose supplier has an activity type in Experio, Tarik turns the withholding on
itself and Experio sets the RAS TVA rate; when that rate depends on the supplier's attestation de régularité
fiscale, Tarik asks for it once: send it as an attachment, or answer « sans attestation ». The same file sent
twice makes one draft only. A sale is imported as a sale (Experio then reads its customer); one billing no TVA is
ticked « TVA non facturée »; a customer with an activity type gets its retenue on, as a supplier does; a hotel's
restaurant, breakfast and laundry amounts are split out where the dossier uses them. The taxe régionale and the taxe de services communaux (TSC) printed on an invoice are checked
against the scan and counted in its TTC: Tarik turns on their setting in the dossier, and asks the cabinet for the TSC
account when the dossier has none. Drafts made by Experio's own
Facturation are posted when you ask for them by name or ask for the whole dossier. A durable good (a computer, a
vehicle, furniture) is proposed on a class 2 account and booked there only once you confirm it; posting then waits
until its account has an asset category in Experio, without which Experio would make no asset. What Tarik did on an
invoice is noted in its history in Experio. A credit note (avoir) is posted as one, and then applied to the invoice it
cancels: the one it names, else the one invoice of that supplier left owing exactly its amount; otherwise the
possible invoices are listed for the cabinet. An invoice Experio took for a credit note is put right when its
printed title says « Facture ».

**Make an import folder**: say « dossier d'import » with the folder's documents (the transit agent's invoice, customs,
transport, the foreign supplier's invoice, the DUM...), and « plusieurs dossiers » for one folder per file. Tarik types
each page as Experio's « Imports » screen does, creates the folder, has Experio read the DUM, and processes each piece
like any invoice: the customs one goes to the dossier's customs tiers, the folder takes its number by the dossier's rule,
and the transit agent's invoice is posted last, with the folder's invoices it paid. The same file sent twice makes one
folder.

**Bank statements**: once imported (a PDF, a photo, or Experio's Excel template; several files of one statement
merged when you say « le même relevé »), Tarik waits for Experio's reading, checks its balances, points its operations
with Experio's own automatic detection (and the invoice you name for a payment), and says what remains to complete;
« comptabilisez » posts its ready operations with Experio's validation and lists those left in suspense. A BTI statement
is refused (Experio does not read them); a duplicate statement Experio flags is left to the cabinet.

**TPE statements** (CMI, NAPS, AL BARID, VPS, ATTIJARI PAYMENT): recognised on their own header and imported
through Experio's own TPE import, with their network (asked when it can't be read) and the journal of the last TPE
statement. Tarik creates a missing TPE operation as Experio does, gives each line its operation (VENTE, FRAIS
BANCAIRES, TAXE BANCAIRE, SOLDE NET REMISE), asks once which client takes the card sales, and gives the total
remise, commissions, TVA and net remise. « comptabilisez » posts it, confirming Experio's warning that the network
changed; a duplicate stops. On a bank statement, TPE remittances are tied to their TPE statement when Experio's own
matching is sure (module « Rapprochement TPE » installed).

**TVA declaration** ("préparez la déclaration de TVA du T3", "la TVA d'août"): Tarik prepares it in Experio for the
period you name (else the last one ended for the dossier's mode): made or taken up, refreshed by Experio, the TVA
credit carried asked on the dossier's first declaration in Experio. It says what the declaration leaves out (drafts of
the period, blocked invoices, unpaid purchases, bank operations in suspense or without tiers), the lines in error and
what Experio refuses, and makes its XML file when Experio's checks pass, attached to Tarik's reply (in the chat: « Fichiers préparés
par Tarik »). "déclarez la TVA" declares
it in Experio (TVA writings, period locked) after you confirm; the deposit on the DGI portal stays yours, and Tarik
never cancels a declaration.

**Withholding declarations (RAS)** ("préparez les RAS de septembre", "la RAS TVA du T3", "la RAS non-résidents
2025"): RAS TVA, RAS IS, RAS IR and RAS IS of non-residents (monthly or yearly), those you name or else those the
dossier files in Experio, monthly unless you name a quarter. Each is made or taken up and refreshed by Experio (never
over lines added by hand), with its totals, what Experio would refuse at « Déclarer » (period not ended, tiers without
ICE or IF, line without activity type or rate, period declared) and what it leaves out (IS/IR withholdings entered on
a bank statement, tiers neither company nor person), and its XML made, which runs Experio's own checks. "déclarez les
RAS" declares them after you confirm; Tarik warns that Experio refuses IR once IS is declared for the period (and the
reverse). The DGI deposit stays yours.

**Payment delays declaration (loi 69-21)** ("préparez les délais de paiement du T3", "la déclaration des délais
2025"): for a dossier that follows payment delays in Experio, quarterly or yearly as the dossier is set. Made or taken
up and refreshed by Experio (never over lines added by hand), the turnover asked when Experio has none (it decides the
declaration the law requires). Tarik gives the lines, suppliers, amounts not yet paid and paid late and the fine, and
says what Experio would refuse (the mode the law requires for that turnover, suppliers without IF, a period declared or
still running) and what it leaves out (purchases it doesn't follow for delays, blocked ones); the XML runs Experio's own
checks. "déclarez les délais de paiement" declares it after you confirm; the DGI deposit stays yours.

**Export** ("exportez les écritures du T3", "le FEC 2026", "export Sage en texte avec ;"): the entries by journal,
the tiers or the FEC, with Experio's own export, in the model and format you name (Excel, or text: a ZIP of one file per
journal with your separator), with reconciliation when you ask. Tarik gives the number of entries and lines and the
total debit and credit, and attaches the file to its email reply (in the chat: « Fichiers préparés par Tarik », to
download). It exports for analysis unless you ask for an integration, which locks the pieces in Experio, after you
confirm; tiers without their own account are asked about first. Experio's refusals (no model, no line, a line without
account) come in its words.

**Write off a payment difference**: what is left owing on a posted invoice ("soldez la facture FV-12"), passed to
the dossier's 6585 account with Experio's own tool, after you confirm the amount and the account.

**Create a dossier**, from its name alone; ICE, IF and type when you give them. It refuses an ICE or IF
another dossier already has, and asks first when a dossier has a very similar name. It can copy the
configuration of a dossier you name, and import its FEC or grand livre.

**Answer its questions.** When processing stops at a question (which account, recoverable TVA,
withholding, fiscal year, which tiers, which bank account for a statement), just answer in the same
conversation, in your own words ("le compte 6145", "oui", "à la charge de la société"): Tarik applies
it and carries on from there. A oui / non question needs a clear oui or non: anything less clear is
asked again. For a tiers, Tarik proposes the closest ones (five at most): choose one, or give another
name or its ICE and it looks again. With several questions waiting, name the invoice (its number or
its supplier) or the statement. Asking Tarik to carry on also resumes invoices Experio was still reading.

**Purchase or sale**: when you don't say it, Tarik reads it on the invoice (who issues it, who it is addressed to,
against the dossier's name) and tells you which it took, so you can correct it; it asks only when the scan doesn't
show it plainly. **A file sent again** is recognised: its invoice is taken up again while still a draft, or said
already posted; nothing is created twice.

**Import a FEC or grand livre** into an existing dossier. Tarik says what Experio's detection created (accounts,
journals, tiers, exercices), refuses a file it already imported into that dossier, sends again with Experio's own
« Renvoyer » the lines still unprocessed an hour after the onboarding is finished, and keeps following the dossier
until Experio has processed every line, its errors told.

**Modify a dossier**: its name, type (société or personne physique), ICE, IF, RC, patente, T.S.C,
capital, CNSS number or address. Only the fields you ask for change, only to the values you write.

## Not yet

- Changing anything else in a dossier: its configuration, accounts, journals, collaborators, currency.
- Deleting a dossier or an invoice: Experio can only archive a dossier, and a posted invoice can't
  simply be deleted.

## How to write to Tarik

Tarik works best when one message says **what to do, for which dossier, with which values**.

**Name the dossier, as it is called in Experio.** A misspelling is fine ("gecafiscc"); a vague name is
not: "les factures du café" matches five dossiers, so Tarik asks which one.

> ✅ Voici les factures d'achat de DJ BUILDING.
> ✅ Voici les factures clients de Gecafisc. *(sales)*
> ❌ Voici mes factures du mois. *(Tarik asks: pour quel dossier, achats ou ventes ?)*

**Say whether invoices are purchases or sales**: "achat" or "fournisseurs" for purchases, "vente" or
"factures clients" for sales, both words for a mix. Tarik never guesses it; when an invoice looks like
the other side, it is left as a draft for you to check.

**Say what you want with a plain verb**: traiter, saisir, enregistrer, comptabiliser (documents);
créer, ouvrir (a dossier); modifier, changer, corriger (a dossier's field).

**Write every value in full, exactly as it should be recorded.** Tarik only uses values you wrote
yourself; it never completes, corrects or guesses one. An ICE has 15 digits, leading zeros included.

> ✅ Créez le dossier ALPHA SARL, société, ICE 008123456000011, IF 71234561.
> ✅ Le RC du dossier Gecafisc est maintenant 331245.
> ❌ Changez le RC de Gecafisc. *(Tarik asks: quel est le nouveau RC ?)*

**For a new dossier, the name is enough.** Tarik creates it at once and does not ask for the ICE or IF
first; give them in the same message when you have them. Say "personne physique" for a person.

**Attach the files to the email that asks for them**, and say for which dossier.

**Some things only happen when you write them yourself**, on purpose:
- Creating or modifying a dossier **without** Experio's ICE/IF check: ask for it ("créez-le sans
  vérification"), after Experio refused the numbers. Tarik then asks you to confirm: answer "oui".
- Copying another dossier's configuration into a new one: name that dossier and ask for its
  configuration ("avec la même configuration que LYJA"). It overwrites the new dossier's settings.

**One request per message works best.** Tarik can do several things in a conversation; one clear
request at a time makes each step easy to follow.

## Good to know

- **Tarik remembers the conversation**, not other conversations: start a new email thread for an
  unrelated request.
- **Each cabinet's Tarik only sees that cabinet**: its dossiers, files and history. Nothing is shared
  between cabinets.
- **A dossier Tarik creates is real.** Experio can't delete a dossier, only archive it: test on test
  dossiers, or on a cabinet in test mode (`DRY_RUN`), where Tarik says "MODE TEST" and posts nothing.
- **Tarik copies Experio's answers word for word**, invoice numbers included: what it reports is what
  Experio did.
