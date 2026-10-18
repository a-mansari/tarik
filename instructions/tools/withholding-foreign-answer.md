---
used_by: settle_withholding (mcp/tarik/invoices/withholding.py)
---
Answer with only a JSON object: {"rule": "none"|"supplier"|"society"|null, "abroad": true|false}. rule: none = sans retenue (1), supplier = a la charge du fournisseur (2), society = a la charge de la societe (3), null when the answer does not say. abroad: the service was rendered outside Morocco ("hors Maroc", "a l'etranger").