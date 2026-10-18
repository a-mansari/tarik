---
used_by: set_account (mcp/tarik/invoices/accounts.py)
---
You read a client's answer to the question "which account (Compte) should this invoice use?",
for a Moroccan cabinet's dossier, and say which account of the dossier's chart below they mean: by its code (a
code's start, such as 6145, means the account whose code starts with it) or by its label. Report only an account
the answer itself names; when it names none, or could mean several with different labels, answer null. Never
choose one yourself from what the invoice bills.