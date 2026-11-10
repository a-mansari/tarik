<!-- The Nouveau cabinet page (an admin's): the form that adds a cabinet, every setting in plain words, in simulation
unless told otherwise. The cabinet is served at once; if Tarik cannot log in to its Experio it is left deactivated and the
form says what to check. -->
<script setup>
import { ref, watch } from "vue";
import { useRouter } from "vue-router";

import { loadSession, send, usePage } from "../api.js";
import Icon from "../components/Icon.vue";
import PageState from "../components/PageState.vue";

const router = useRouter();
const { data, status, loading } = usePage(() => "/settings/new");
const name = ref("");
const form = ref({});
const problem = ref("");
const saving = ref(false);

// A field as the form starts it: a secret and a text empty, a flag in simulation, an adapter the first.
function start(f, adapters) {
  if (f.kind === "flag") return "1";
  if (f.kind === "adapter") return Object.keys(adapters)[0];
  return "";
}

watch(data, (shown) => {
  const values = {};
  for (const [, items] of shown?.sections || []) {
    for (const f of items) values[f.key] = start(f, shown.adapters);
  }
  form.value = values;
});

const REAL_WRITES = "Tarik écrira réellement dans l'Experio de ce cabinet (brouillons, comptabilisations, dossiers). "
  + "Continuer ?";

async function add() {
  if (form.value.DRY_RUN === "0" && !confirm(REAL_WRITES)) return;
  saving.value = true;
  const answer = await send("/settings/new", { name: name.value, ...form.value });
  saving.value = false;
  if (!answer.ok) {
    problem.value = answer.data?.error || "Non enregistré.";
    scrollTo({ top: 0 });
    return;
  }
  problem.value = "";
  await send("/cabinet", { cabinet: name.value.trim() }); // the new cabinet picked in the sidebar
  await loadSession();
  router.push("/ui/accueil");
}
</script>

<template>
  <PageState v-if="!data || status === 403" :status="status" :loading="loading" />
  <template v-else>
    <div class="head"><div>
      <h1>Nouveau cabinet</h1>
    </div>
      <div class="head-tools">
        <button type="submit" form="new-cabinet-form" class="btn btn-primary btn-icon" :disabled="saving"
          title="Ajouter le cabinet" aria-label="Ajouter le cabinet"><Icon name="check" /></button>
      </div>
    </div>
    <div v-if="problem" class="panel panel-bad" role="alert"><b>Non ajouté :</b> {{ problem }}</div>
    <form id="new-cabinet-form" class="content-form" autocomplete="off" @submit.prevent="add">
      <details class="panel card" open>
        <summary><span class="card-title">Cabinet</span></summary>
        <div class="card-body"><div class="field-grid">
          <label class="field">
            <span>Base Experio <span class="mono muted">name</span></span>
            <input v-model="name" name="name" placeholder="experio_cabinet_60" required>
          </label>
        </div></div>
      </details>
      <details v-for="[title, items] in data.sections" :key="title" class="panel card" open>
        <summary><span class="card-title">{{ title }}</span></summary>
        <div class="card-body"><div class="field-grid">
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
            >
            <input
              v-else
              v-model="form[f.key]"
              :name="f.key"
              :inputmode="f.kind === 'integer' ? 'numeric' : undefined"
            >
          </label>
        </div></div>
      </details>
    </form>
  </template>
</template>

<style scoped>
.head-tools { display: flex; align-items: center; gap: var(--space-4); }
</style>
