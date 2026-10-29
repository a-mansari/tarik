<!-- The Comportement page: how Tarik behaves for the cabinet picked in the sidebar, each switch or delay saved at once.
A manager can ask Tarik for the same changes in a conversation; the page shows who changed each one last. -->
<script setup>
import { ref } from "vue";

import { send, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import PageState from "../components/PageState.vue";

const { data, status, loading } = usePage(() => "/comportement");
const busy = ref(false);
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
      <h1>Comportement · <CabinetLabel :name="data.cabinet" /></h1>
      <p class="what">Comment Tarik se comporte pour ce cabinet. Un changement s'applique dès l'email suivant ; un
        manager peut aussi le demander à Tarik dans une conversation.</p>
    </div></div>
    <div v-if="data.error" class="panel bad" role="alert"><b>Non enregistré :</b> {{ data.error }}.</div>
    <article v-for="setting in data.settings" :key="`${setting.key}-${shown}`" class="panel card">
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
        <p class="muted"><template v-if="setting.changed_by">Changé par {{ setting.changed_by }} le
          <span class="num">{{ setting.changed_at }}</span></template><template v-else>Réglage par défaut</template></p>
      </div>
    </article>
  </template>
</template>
