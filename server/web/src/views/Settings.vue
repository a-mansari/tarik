<!-- The Réglages page: the settings of the cabinet picked in the sidebar, in plain words, each change kept (who, when).
A password or a key is never sent here: only whether it is set, and a field to replace it. Switching a cabinet to real
writes in Experio is confirmed first. -->
<script setup>
import { computed, ref, watch } from "vue";

import { plural, send, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import PageState from "../components/PageState.vue";

const { data, status, loading } = usePage(() => "/settings");
const form = ref({});
const was = ref({});

// A field as the form starts it: a secret empty (it is only replaced), a flag 1 unless 0, an adapter unset the first.
function start(f, adapters) {
  if (f.kind === "secret") return "";
  if (f.kind === "flag") return f.value === "0" ? "0" : "1";
  if (f.kind === "adapter") return f.value || Object.keys(adapters)[0];
  return f.value;
}

watch(data, (shown) => {
  const values = {};
  for (const [, items] of shown?.sections || []) {
    for (const f of items) values[f.key] = start(f, shown.adapters);
  }
  form.value = values;
  was.value = { ...values }; // as the page showed them: switching to real writes is confirmed against these
});

// The cabinet switched from simulation to real writes in Experio by this form.
const goingReal = () => was.value.DRY_RUN !== "0" && form.value.DRY_RUN === "0";
const REAL_WRITES = "Tarik écrira réellement dans l'Experio de ce cabinet (brouillons, comptabilisations, dossiers). "
  + "Continuer ?";

async function save() {
  if (goingReal() && !confirm(REAL_WRITES)) return;
  const answer = await send("/settings", { ...form.value, version: String(data.value.version), cabinet: data.value.cabinet });
  if (answer.data) data.value = answer.data;
  scrollTo({ top: 0 });
}

// The page's head: the cabinet's mode, when it is known.
const modeKnown = computed(() => data.value.dry_run !== null && data.value.dry_run !== undefined);
const savedNames = computed(() => data.value.saved.map((key) => data.value.names[key]).join(", "));

const placeholder = (f) => (f.value ? "Défini — laisser vide pour le garder" : "Non défini");
function trimHint(f) {
  if (f.kind === "secret") return "Gardé exactement comme tapé, espaces compris.";
  return "Les espaces au début et à la fin sont retirés ; le reste est gardé comme tapé.";
}
</script>

<template>
  <PageState v-if="!data || status === 403 || status === 404" :status="status" :loading="loading" />
  <template v-else>
    <div class="head"><div>
      <h1>Réglages · <CabinetLabel :name="data.cabinet" /><template v-if="modeKnown"> <span
        class="chip"
        :class="data.dry_run ? '' : 'wait'"
      >{{ data.dry_run ? "Simulation" : "Écritures réelles" }}</span></template></h1>
      <p class="what">Ce que Tarik utilise pour ce cabinet. Un mot de passe ou une clé n'est jamais affiché : il se remplace seulement. Un changement s'applique dès le message suivant.</p>
    </div></div>
    <div v-if="data.unreachable" class="panel bad" role="alert">
      <b>Réglages illisibles.</b> Vault ne répond pas, ou le rôle de cette page (tarik-settings) n'est pas installé :
      <span class="mono">vault/setup.sh</span>.</div>
    <template v-else>
      <div v-if="data.error" class="panel bad" role="alert"><b>Non enregistré :</b> {{ data.error }}.</div>
      <div v-if="data.saved" class="panel accent" role="status">
        <template v-if="data.saved.length">Enregistré : {{ savedNames }}. En service dès maintenant.</template>
        <template v-else>Rien n'a changé.</template>
      </div>
      <div class="flow content-flow">
        <form id="settings-form" class="content-form" autocomplete="off" @submit.prevent="save">
          <details v-for="[title, items] in data.sections" :key="title" class="panel card" open>
            <summary>
              <span class="card-title">{{ title }}</span>
              <span class="card-meta"><span class="muted">{{ items.length }} réglage{{ plural(items.length) }}</span></span>
            </summary>
            <div class="card-body">
              <label v-for="f in items" :key="f.key" class="field">
                <span>{{ f.name }} <span class="mono muted">{{ f.key }}</span><template
                  v-if="f.optional"> <span class="muted">(facultatif)</span></template></span>
                <select v-if="f.kind === 'flag'" v-model="form[f.key]" :name="f.key">
                  <option value="1">Simulation : Tarik dit ce qu'il ferait, rien n'est écrit dans Experio</option>
                  <option value="0">Écritures réelles : Tarik crée et modifie dans Experio</option>
                </select>
                <select v-else-if="f.kind === 'adapter'" v-model="form[f.key]" :name="f.key">
                  <option v-for="(label, key) in data.adapters" :key="key" :value="key">{{ label }}</option>
                </select>
                <input
                  v-else-if="f.kind === 'secret'"
                  v-model="form[f.key]"
                  type="password"
                  :name="f.key"
                  autocomplete="new-password"
                  :placeholder="placeholder(f)"
                >
                <input
                  v-else
                  v-model="form[f.key]"
                  :name="f.key"
                  :inputmode="f.kind === 'integer' ? 'numeric' : undefined"
                >
                <small v-if="f.description" class="muted">{{ f.description }}</small>
                <small class="muted trim">{{ trimHint(f) }}</small>
              </label>
            </div>
          </details>
        </form>
        <div class="side-column">
          <section class="panel save-card" aria-label="Enregistrer">
            <button type="submit" form="settings-form" class="primary">Enregistrer</button>
          </section>
          <aside class="side panel">
            <h2>Historique</h2>
            <ol v-if="data.history.length" class="versions settings-history">
              <li v-for="item in data.history" :key="item.version + item.at"><div class="entry">
                <span class="num">{{ item.at }}</span> <b>{{ item.author }}</b>
                <ul>
                  <li v-for="change in item.changes" :key="change.key">
                    <b>{{ data.names[change.key] || change.key }}</b>
                    <template v-if="change.before === null"> : remplacé</template>
                    <template v-else> : <span class="mono">{{ change.before || "(vide)" }}</span>
                      → <span class="mono">{{ change.after || "(vide)" }}</span></template>
                  </li>
                </ul>
              </div></li>
            </ol>
            <p v-else class="muted">Aucun changement fait depuis cette page.</p>
          </aside>
        </div>
      </div>
    </template>
  </template>
</template>
