<!-- The Réglages page: the settings of the cabinet picked in the sidebar, in plain words, each change kept (who, when).
A password or a key is never sent here: only whether it is set, and a field to replace it. Switching a cabinet to real
writes in Experio is confirmed first. -->
<script setup>
import { computed, ref, watch } from "vue";

import { loadSession, send, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import PageState from "../components/PageState.vue";
import Icon from "../components/Icon.vue";
import SettingField from "../components/SettingField.vue";
import { REAL_WRITES, startValues, visible } from "../settingsForm.js";

const { data, status, loading, reload } = usePage(() => "/settings");
const problem = ref("");
const form = ref({});
const was = ref({});

watch(data, (shown) => {
  const values = startValues(shown);
  form.value = values;
  was.value = { ...values }; // as the page showed them: switching to real writes is confirmed against these
});

// The cabinet switched from simulation to real writes in Experio by this form.
const goingReal = () => was.value.DRY_RUN !== "0" && form.value.DRY_RUN === "0";

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
            <div class="card-body"><div class="field-grid">
              <SettingField v-for="f in items.filter((one) => visible(one, form))" :key="f.key" v-model="form[f.key]" :f="f" />
            </div></div>
          </details>
      </form>
    </template>
  </template>
</template>

<style scoped>
.head-tools { display: flex; align-items: center; gap: var(--space-4); }
</style>
