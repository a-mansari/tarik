# Experio's invoice warnings and RAS rules

Proven from Experio's code (`experio_app`, `addons/invoice/models/exp_invoice.py`, module `invoice` 18.0.1.5.85, the
version the test replicas run; upstream master v2.9.17 has the same warning list and codes) and, for the RAS tables, a
live run on experio_cabinet_8 (dossier 857, 2026-10-23: 120 cases through Experio's own onchange and check, test data
removed after). Line numbers are of that version.

## How warnings work

- `collect_validation_messages` (4286) returns `(level, text)`: `cgnc` (dossier setting), `error`, `warning`.
- `action_post` (4414-4421) raises on `cgnc` and `error` only. **Warnings never block posting** in Experio.
- Each warning check writes its `warning_code` on the invoice before raising: calling the check is a write.
- `action_dismiss_warning` (3697) adds the invoice's current `warning_code` to `dismissed_warning_codes` and posts
  « Warning deactivated: … » in the chatter. One code per call: the one the last raising check wrote.
- `validate_folder_dum_detection` raises without a code of its own: it cannot be dismissed by code.
- `validate_warning_payment_delay` (declaration module) is commented out of both lists: inactive.

## The warnings, their handling, and whether Tarik can do it

| Check (line) | Code(s) | When | Handling | Tarik, automated? |
|---|---|---|---|---|
| withholding (7554) | `missing_vat_amount_for_ras` | non-recoverable TVA, TVA 0, retenue, RAS TVA ≠ 0 | the TVA amount is missing | yes when the scan shows the TVA (correct_invoice), else ask |
| withholding (7554-8030) | 21 RAS TVA codes (table below) | RAS TVA % differs from Experio's rule | Experio's onchange on partner_id / retenue / amount_total (7838) sets the right % itself | yes: set the tiers' `expense_type`, get its regularity certificate, let the onchange set the %; never type the % |
| folder DUM (8064) | none | the import folder's `error_msg` (DUM rate not found) | the DUM's exchange rate | no: customs folders, the cabinet's |
| end of month rate (8074) | `exchange_rate_may_change_month_not_ended` | dossier on `monthly_average` rates, invoice in the current month | wait for the month's end | yes: keep the draft, take it up after the month ends |
| today's rate (8093) | `today_exchange_rate_not_available` | foreign currency, dated today | Experio used the day before's rate | yes: take it up the next day |
| prorata (8105) | `prorata_vat_rate_not_applied_on_invoice` | dossier `use_prorata`, invoice `tax_prorata` off | tick prorata, where the form shows it: local purchase, recoverable TVA (views 786-789) | yes: tick it (settle_tva); on a sale, a foreign purchase or non-recoverable TVA the field is hidden: dismiss |
| ICE/IF (8118) | `company_ice_matches_detected_reverse_partner_ice`, `…_if` | the issuer read is the dossier itself | wrong side (a sale imported as a purchase) | yes: switch the side, unless the client gave it (then ask) |
| ICE/IF (8118) | `detected_ice_mismatch_with_company_record`, `…_if` | the buyer read ≠ the dossier's ICE/IF (only when the dossier has one) | invoice of another company, or the dossier's ICE wrong | ask the client; never change the dossier's ICE from a scan |
| account (8191) | `purchase_invoice_account_outside_class_2_and_6`, `sale_invoice_account_outside_class_7` | account class | change the account | yes: choose only in those classes; ask when the memorized one is outside |
| previous charges (8219) | `invoice_date_outside_fiscal_entry` | date outside the invoice's exercice | relink, late invoice, or new exercice | yes (settle_exercice) |
| local withholding (8279) | `missing_untaxed_amount_for_is_ir_withholding` | non-recoverable TVA, HT 0, IS/IR rate | the HT amount is missing | yes from the scan, else ask |
| local withholding (8279) | `company_local_withholding_rate_not_valid_for_is_tax`, `individual_…_for_ir_tax` | IS/IR rate not allowed for the tiers' type | company: 5, 10, 20, 30 %; person: 10, 15, 20 % | yes: check the client's rate against the tiers' type, ask again |

Level `error`, though it writes a warning code: `disbursement_liquidation_accounts_not_configured`,
`regional_tax_liquidation_accounts_not_configured` (8533; sales of `use_transitaire` dossiers): accounts to choose in
Configuration → General → Sale, the cabinet's.

## RAS TVA: the % Experio's onchange sets

Applies only when `retenue` is on, the invoice is dated from 2024-07-01, and it is not a foreign purchase
(`should_use_tax_withholding`, 495; `_is_tax_withholding_eligible_type`, 10209). Before 2024-07-01 the onchange leaves
the % as it is, and the writing (6597) still uses it.

Regularity: an `exp.partner.regularity.form` of the tiers, `state` active and `regularity_date <= invoice date <
regularity_end_date` (end = date + `regularity_validity_period` months). An expired one counts as none.

Dossier not public (`exp_is_public_organization` off):

| Tiers | expense_type | no regularity | regularity |
|---|---|---|---|
| company or person | none, loyer | 0 | 0 |
| company or person | equipment, travaux | 100 | 0 |
| company | prestation | 0 | 0 |
| person | prestation, TTC ≤ 5 000 and month ≤ 50 000 | 0 | 0 |
| person | prestation, TTC > 5 000, or month > 50 000 | 100 | 75 |

« month »: the tiers' posted invoices of the invoice's month plus this one (`should_use_service_regularity`, 7845).
That branch was not run live (it needs posted invoices); the code is 7940-8030.

Public dossier: 0 in every case, except a person's prestation over the thresholds, which gets 100 / 75 as above.
There Experio contradicts itself (7876-7905: the public branch does not return): every % is warned, live-confirmed.

`loyer` has no rule of its own: it gets the « sans type d'activité » warning text (live).

## IS/IR (local withholding) rates Experio accepts (8279, live)

| Tiers | accepted | warned |
|---|---|---|
| company | 5, 10, 20, 30 | 15 |
| person | 10, 15, 20 | 5, 30 |
