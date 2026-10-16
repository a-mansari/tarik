# Using Tarik

What Tarik can do for a cabinet, and how to write to it for the best results. Tarik answers in the
language you write in, in short sentences.

## Where to reach it

| | Chat | Email |
|---|---|---|
| Where | The cabinet's agent, "Tarik - <cabinet>", in n8n's Agents section | The cabinet's own Tarik mailbox |
| Files accepted | Images and PDFs only, about 1 MB each | PDF, images, CSV, TXT, XLSX: any size the mail server accepts |
| Who may use it | People with access to the agent in n8n | Only the cabinet's whitelisted addresses (`@experio.ma`, `@sarnec.com`, …), as the email's From says; for an existing dossier, a collaborator (or one of Experio's own staff) who has that dossier in Experio. A colleague replying in your thread gets their own answer. |
| When it answers | Right away | Within seconds of the email arriving (15 minutes at most) |

Send FEC and grand livre files, and large scans, **by email**: the chat drops them without a word.

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
fiscal year, and posts it. When a step needs you (which account? which fiscal year?), it asks.

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
- FEC or grand livre files through the chat (use email).

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

**Attach the files to the message that asks for them.** In the chat, attach invoices (PDF or photos)
and say for which dossier; by email, attach them to the email.

**Some things only happen when you write them yourself**, on purpose:
- Creating or modifying a dossier **without** Experio's ICE/IF check: ask for it ("créez-le sans
  vérification"), after Experio refused the numbers. Tarik then asks you to confirm: answer "oui".
- Copying another dossier's configuration into a new one: name that dossier and ask for its
  configuration ("avec la même configuration que LYJA"). It overwrites the new dossier's settings.

**One request per message works best.** Tarik can do several things in a conversation; one clear
request at a time makes each step easy to follow.

## Good to know

- **Tarik remembers the conversation**, not other conversations: start a new chat, or a new email
  thread, for an unrelated request.
- **Each cabinet's Tarik only sees that cabinet**: its dossiers, files and history. Nothing is shared
  between cabinets.
- **A dossier Tarik creates is real.** Experio can't delete a dossier, only archive it: test on test
  dossiers, or on a cabinet in test mode (`DRY_RUN`), where Tarik says "MODE TEST" and posts nothing.
- **Tarik copies Experio's answers word for word**, invoice numbers included: what it reports is what
  Experio did.
