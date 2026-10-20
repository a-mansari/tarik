"""Every tool module, imported so each registers its tools (tarik.core.app.TOOLS)."""

from tarik.core import experio
from tarik.dossiers import identity, management, people, search, setup
from tarik.emails import inbox, sending
from tarik.invoices import (
    accounts,
    check,
    classify,
    configuration,
    correct,
    duplicates,
    exercice,
    intake,
    journal,
    listing,
    pipeline,
    posting,
    prepaid,
    tiers,
    tva,
    withholding,
)

MODULES = (
    experio,
    identity,
    management,
    people,
    search,
    setup,
    inbox,
    sending,
    accounts,
    check,
    classify,
    configuration,
    correct,
    duplicates,
    exercice,
    intake,
    journal,
    listing,
    pipeline,
    posting,
    prepaid,
    tiers,
    tva,
    withholding,
)
