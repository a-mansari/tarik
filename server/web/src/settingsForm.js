// What the cabinet forms share (the Réglages page and the Ajouter un cabinet page): how a field starts, and the
// confirmation before a cabinet writes for real in Experio.

// A field as the form starts it: a secret empty (it is only replaced), a flag 1 (simulation) or a toggle 1 (on) unless
// stored as 0, a choice (an LLM) the stored one or the first.
export function startOf(f) {
  if (f.kind === "secret") return "";
  if (f.kind === "flag" || f.kind === "toggle") return f.value === "0" ? "0" : "1";
  if (f.kind === "choice") return f.value in f.choices ? f.value : Object.keys(f.choices)[0]; // one removed: the first
  return f.value;
}

// The form's values for every field of the page's sections.
export function startValues(shown) {
  const values = {};
  for (const [, items] of shown?.sections || []) {
    for (const f of items) values[f.key] = startOf(f);
  }
  return values;
}

export const REAL_WRITES = "Tarik écrira réellement dans l'Experio de ce cabinet (brouillons, comptabilisations, dossiers). "
  + "Continuer ?";

// A field is shown when the form holds what it asks (its "when": [key, value] pairs): the cabinet's own LLM only when
// the cabinet picks it.
export const visible = (f, form) => (f.when || []).every(([key, value]) => form[key] === value);
