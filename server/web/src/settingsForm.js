// What the cabinet forms share (the Réglages page and the Ajouter un cabinet page): how a field starts, and the
// confirmation before a cabinet writes for real in Experio.

// A field as the form starts it: a secret empty (it is only replaced), a flag 1 (simulation) unless stored as 0, an
// adapter the stored one or the first.
export function startOf(f, adapters) {
  if (f.kind === "secret") return "";
  if (f.kind === "flag") return f.value === "0" ? "0" : "1";
  if (f.kind === "adapter") return f.value || Object.keys(adapters)[0];
  return f.value;
}

// The form's values for every field of the page's sections.
export function startValues(shown) {
  const values = {};
  for (const [, items] of shown?.sections || []) {
    for (const f of items) values[f.key] = startOf(f, shown.adapters);
  }
  return values;
}

export const REAL_WRITES = "Tarik écrira réellement dans l'Experio de ce cabinet (brouillons, comptabilisations, dossiers). "
  + "Continuer ?";
