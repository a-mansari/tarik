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
restaurant, breakfast and laundry amounts are split out where the dossier uses them. Drafts made by Experio's own
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
what Experio refuses, and makes its XML file on the declaration when Experio's checks pass. "déclarez la TVA" declares
it in Experio (TVA writings, period locked) after you confirm; the deposit on the DGI portal stays yours, and Tarik
never cancels a declaration.

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

**Import a FEC or grand livre** into an existing dossier.

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
