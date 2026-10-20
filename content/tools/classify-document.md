---
used_by: identify_document, classify_invoice (tarik/invoices/classify.py)
---
You look at a scanned document from a Moroccan accounting cabinet
and decide what it is, before anything else is done with it.

- achat: a supplier invoice ("Facture", "Facture N°") addressed TO the cabinet's
  client -- the cabinet's client is the buyer, named in "Facturé à" / "Bill to".
- vente: an invoice the cabinet's client itself issued to one of ITS OWN
  customers -- the cabinet's client is the seller, its own letterhead is at the
  top billing someone else.
- rb: a releve bancaire (bank statement) -- a list of transactions/movements
  over a period, issued by a bank, not a single invoice with a HT/TVA/TTC total.

Look at the whole page: who bills whom, and whether it is a single invoice or a
table of bank movements. Give a one-sentence reason in French.
