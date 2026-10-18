---
used_by: settle_withholding (mcp/tarik/invoices/withholding.py)
---
Answer with only a JSON object: {"retenue": true|false|null, "is_ir": 5|10|15|20|30|null, "tva": 0|75|100}. retenue: false for no withholding ("sans", "non"), true when the answer gives a rate, null when it does not say. is_ir: the IS/IR withholding rate it gives, else null. tva: the TVA withholding rate it gives, else 0.