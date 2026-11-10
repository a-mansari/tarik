<!-- The Ajouter un cabinet page (an admin's): the form that adds a cabinet, every setting in plain words, in
simulation unless told otherwise. Tarik logs in to the cabinet's Experio first; the cabinet is served only when that
works, else it is left deactivated and the form says what to check. -->
<script setup>
import { ref, watch } from "vue";
import { useRouter } from "vue-router";

import { loadSession, send, usePage } from "../api.js";
import Icon from "../components/Icon.vue";
import PageState from "../components/PageState.vue";
import SettingField from "../components/SettingField.vue";
import { REAL_WRITES, startValues, visible } from "../settingsForm.js";

const router = useRouter();
const { data, status, loading } = usePage(() => "/settings/new");
const name = ref("");
const form = ref({});
const problem = ref("");
const saving = ref(false);

watch(data, (shown) => {
  form.value = startValues(shown);
});

async function add() {
  if (form.value.DRY_RUN === "0" && !confirm(REAL_WRITES)) return;
  saving.value = true;
  const answer = await send("/settings/new", { name: name.value, ...form.value });
  saving.value = false;
  await loadSession(); // the sidebar lists the cabinet, deactivated or not, as soon as it exists
  if (!answer.ok) {
    problem.value = answer.data?.error || "Non enregistré.";
    scrollTo({ top: 0 });
    return;
  }
  problem.value = "";
  const picked = await send("/cabinet", { cabinet: name.value.trim() }); // the new cabinet picked in the sidebar
  if (picked.ok) await loadSession();
  router.push("/ui/accueil");
}
</script>

<template>
  <PageState v-if="!data || status === 403" :status="status" :loading="loading" />
  <template v-else>
    <div class="head"><div>
      <h1>Ajouter un cabinet</h1>
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
            <small class="muted">Le nom exact de la base Experio : lettres minuscules, chiffres et _, en commençant par une lettre.</small>
          </label>
        </div></div>
      </details>
      <details v-for="[title, items] in data.sections" :key="title" class="panel card" open>
        <summary><span class="card-title">{{ title }}</span></summary>
        <div class="card-body"><div class="field-grid">
          <SettingField v-for="f in items.filter((one) => visible(one, form))" :key="f.key" v-model="form[f.key]" :f="f" />
        </div></div>
      </details>
    </form>
  </template>
</template>

<style scoped>
.head-tools { display: flex; align-items: center; gap: var(--space-4); }
</style>
