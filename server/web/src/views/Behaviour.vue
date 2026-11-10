<!-- The Comportement page: how Tarik behaves for the cabinet picked in the sidebar, each switch or delay saved at
once, in two parts: the cabinet's own settings, and those of its dossiers.
A manager can ask Tarik for the same changes in a conversation; the page shows who changed each one last. -->
<script setup>
import { computed, ref } from "vue";

import { send, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import PageState from "../components/PageState.vue";

const { data, status, loading } = usePage(() => "/comportement");
const busy = ref(false);
const SCOPES = [
  { scope: "cabinet", title: "Réglages du cabinet", what: "Valent pour tous les clients du cabinet." },
  { scope: "dossier", title: "Réglages des dossiers", what: "Valent pour chaque dossier qui n'a pas son propre choix, fait dans le formulaire envoyé au client pour un nouveau dossier. La TVA du dossier n'est pas ici : elle reste dans Experio." },
];
const parts = computed(() => SCOPES.map((part) => ({ ...part, settings: data.value.settings.filter((s) => s.scope === part.scope) })));
const shown = ref(0); // bumped to draw the controls again as saved, after a refusal

async function save(setting, value) {
  busy.value = true;
  const answer = await send("/comportement", { cabinet: data.value.cabinet, key: setting.key, value: String(value) });
  busy.value = false;
  if (answer.data) data.value = answer.data;
  shown.value += 1;
}
</script>

<template>
  <PageState v-if="!data || status === 403 || status === 404" :status="status" :loading="loading" />
  <template v-else>
    <div class="head"><div>
      <h1><CabinetLabel :name="data.cabinet" /> · Comportement</h1>
    </div></div>
    <div v-if="data.error" class="panel panel-bad" role="alert"><b>Non enregistré :</b> {{ data.error }}.</div>
    <section v-for="part in parts" :key="part.scope">
      <h2>{{ part.title }}</h2>
      <p class="what">{{ part.what }}</p>
      <div class="field-grid">
      <article v-for="setting in part.settings" :key="`${setting.key}-${shown}`" class="panel card setting">
        <div class="card-body">
          <label v-if="setting.kind === 'flag'" class="switch">
            <input type="checkbox" role="switch" :checked="setting.value" :disabled="busy"
              @change="save(setting, $event.target.checked ? 1 : 0)">
            <b>{{ setting.name }}</b>
          </label>
          <label v-else class="field">
            <b>{{ setting.name }}</b>
            <input type="number" min="0" max="120" inputmode="numeric" :value="setting.value" :disabled="busy"
              @change="save(setting, $event.target.value)">
          </label>
          <p class="muted">{{ setting.description }}</p>
        </div>
      </article>
      </div>
    </section>
  </template>
</template>

<style scoped>
/* One padding per card (the body's, not the panel's on top of it); the cards sit on the field grid. */
section .what { margin: var(--space-1) 0 var(--space-4); }
.setting { padding: 0; }
.setting .card-body { gap: var(--space-1); padding: var(--space-3) var(--space-5); }
</style>
