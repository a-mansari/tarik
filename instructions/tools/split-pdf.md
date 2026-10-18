---
used_by: split_invoices (mcp/tarik/invoices/intake.py)
---
You are given the pages of one scanned PDF, in order, from a Moroccan
accounting cabinet. It may hold several invoices. Say on which pages a NEW
invoice begins.

A page begins a new invoice when it has its own header: a supplier letterhead,
"Facture N°", a date, a "Facture a" / client box. A page continues the previous
one when it has no such header and goes on with the same lines, or only
carries the totals (HT, TVA, TTC), the signature or the terms.

Page 1 always begins an invoice. When unsure, a page begins a new invoice.
