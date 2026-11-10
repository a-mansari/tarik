<!-- The Réglages page: the settings of the cabinet picked in the sidebar, in plain words, each change kept (who, when).
A password or a key is never sent here: only whether it is set, and a field to replace it. Switching a cabinet to real
writes in Experio is confirmed first. -->
<script setup>
import { computed, ref, watch } from "vue";

import { loadSession, send, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import PageState from "../components/PageState.vue";
import Icon from "../components/Icon.vue";

const { data, status, loading, reload } = usePage(() => "/settings");
const problem = ref("");
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

// Switch a cabinet on or off, at once: off, Tarik reads none of its mail and starts none of its runs. The sidebar and
// this page are read again, a cabinet switched off leaving both.
const switching = ref(false);
async function activate(cabinet, on) {
  switching.value = true;
  const answer = await send("/settings/active", { cabinet, active: on ? "1" : "0" });
  problem.value = answer.ok ? "" : answer.data?.error || "Non enregistré.";
  await loadSession();
  await reload();
  switching.value = false;
}

const savedNames = computed(() => data.value.saved.map((key) => data.value.names[key]).join(", "));

const placeholder = (f) => (f.value ? "Défini — laisser vide pour le garder" : "Non défini");
</script>

<template>
  <PageState v-if="!data || status === 403 || status === 404" :status="status" :loading="loading" />
  <template v-else>
    <div class="head"><div>
      <h1><CabinetLabel :name="data.cabinet" /> · Réglages</h1>
    </div>
      <div class="head-tools">
        <button v-if="data.active && !data.unreachable" type="submit" form="settings-form" class="btn btn-primary btn-icon"
          title="Enregistrer" aria-label="Enregistrer"><Icon name="check" /></button>
      <label class="switch" :title="data.active ? 'Actif : cliquer pour désactiver' : 'Désactivé : cliquer pour activer'">
        <input type="checkbox" role="switch" :checked="data.active" :disabled="switching"
          @change="activate(data.cabinet, $event.target.checked)">
        <Icon name="power" :size="18" /><span class="sr-only">{{ data.active ? "Actif" : "Désactivé" }}</span>
      </label>
      </div>
    </div>
    <div v-if="problem" class="panel panel-bad" role="alert"><b>Non enregistré :</b> {{ problem }}</div>
    <div v-if="data.active && data.unreachable" class="panel panel-bad" role="alert">
      <b>Réglages illisibles.</b> Vault ne répond pas, ou le rôle de cette page (tarik-settings) n'est pas installé :
      <span class="mono">vault/setup.sh</span>.</div>
    <p v-else-if="!data.active" class="muted">Cabinet désactivé : activez-le pour modifier ses réglages.</p>
    <template v-else>
      <div v-if="data.error" class="panel panel-bad" role="alert"><b>Non enregistré :</b> {{ data.error }}.</div>
      <div v-if="data.saved" class="panel panel-accent" role="status">
        <template v-if="data.saved.length">Enregistré : {{ savedNames }}. En service dès maintenant.</template>
        <template v-else>Rien n'a changé.</template>
      </div>
      <form id="settings-form" class="content-form" autocomplete="off" @submit.prevent="save">
          <details v-for="[title, items] in data.sections" :key="title" class="panel card" open>
            <summary>
              <span class="card-title">{{ title }}</span>
            </summary>
            <div class="card-body">
              <label v-for="f in items" :key="f.key" class="field">
                <span>{{ f.name }} <span class="mono muted">{{ f.key }}</span><template
                  v-if="f.optional"> <span class="muted">(facultatif)</span></template></span>
                <select v-if="f.kind === 'flag'" v-model="form[f.key]" :name="f.key">
                  <option value="1">Simulation</option>
                  <option value="0">Écritures réelles</option>
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
              </label>
            </div>
          </details>
      </form>
    </template>
  </template>
</template>

<style scoped>
.head-tools { display: flex; align-items: center; gap: var(--space-4); }
/* Related fields share a line (service logins side by side), each as wide as it needs, wrapping on a narrow screen. */
#settings-form .card-body { grid-template-columns: repeat(auto-fit, minmax(min(100%, 16rem), 22rem)); align-items: start; }
</style>
