<!-- The Paramètres page: what every cabinet shares (the mail server, the model, the Experio key, Gladden, Sparrow), in
plain words, each change kept (who, when) by the server. A key or a token is never sent here: only whether it is set, and a field to
replace it. -->
<script setup>
import { computed, ref, watch } from "vue";

import { send, usePage } from "../api.js";
import Icon from "../components/Icon.vue";
import LlmList from "../components/LlmList.vue";
import PageState from "../components/PageState.vue";
import SettingField from "../components/SettingField.vue";

const { data, status, loading } = usePage(() => "/settings/general");
const form = ref({});
const was = ref({});

// A field as the form starts it: a secret empty (it is only replaced).
watch(data, (shown) => {
  const values = {};
  for (const [, items] of shown?.sections || []) {
    for (const f of items) values[f.key] = f.kind === "secret" ? "" : f.value;
  }
  form.value = values;
  was.value = { ...values };
});

// Where Tarik sends its mailboxes' passwords, the clients' documents, the links it emails, or the model's key: a change
// is confirmed first, a wrong address would hand them over.
const SENT_OUT = ["IMAP_HOST", "SMTP_HOST", "MAIL_CERT_SHA256", "GLADDEN_URL", "PUBLIC_URL", "MODEL_API_URL", "SPARROW_URL"];
const WHERE_TO = "Tarik enverra les mots de passe des boîtes mail, les pièces des clients, les liens de formulaire ou "
  + "la clé du modèle à ces adresses. Continuer ?";

async function save() {
  const moved = SENT_OUT.filter((key) => form.value[key] !== was.value[key]);
  if (moved.length && !confirm(`${moved.map((key) => data.value.names[key]).join(", ")} : ${WHERE_TO}`)) return;
  const answer = await send("/settings/general", { ...form.value, version: data.value.version });
  if (answer.data) data.value = answer.data;
  scrollTo({ top: 0 });
}

const savedNames = computed(() => data.value.saved.map((key) => data.value.names[key]).join(", "));
</script>

<template>
  <PageState v-if="!data || status === 403" :status="status" :loading="loading" />
  <template v-else>
    <div class="head">
      <h1>Paramètres</h1>
      <button v-if="!data.unreachable" type="submit" form="general-form" class="btn btn-primary btn-icon" title="Enregistrer"
        aria-label="Enregistrer"><Icon name="check" /></button>
    </div>
    <div v-if="data.unreachable" class="panel panel-bad" role="alert">
      <b>Paramètres illisibles.</b> Vault ne répond pas, ou le rôle de cette page (tarik-settings) n'a pas le droit de
      lire ces réglages : relancer <span class="mono">vault/setup.sh</span>.</div>
    <template v-else>
      <div v-if="data.error" class="panel panel-bad" role="alert"><b>Non enregistré :</b> {{ data.error }}.</div>
      <div v-if="data.saved" class="panel panel-accent" role="status">
        <template v-if="data.saved.length">Enregistré : {{ savedNames }}. En service dès maintenant.</template>
        <template v-else>Rien n'a changé.</template>
      </div>
      <form id="general-form" class="content-form" autocomplete="off" @submit.prevent="save">
          <details v-for="[title, items] in data.sections" :key="title" class="panel card" open>
            <summary>
              <span class="card-title">{{ title }}</span>
            </summary>
            <div class="card-body"><div class="field-grid">
              <SettingField v-for="f in items" :key="f.key" v-model="form[f.key]" :f="f" />
            </div></div>
          </details>
      </form>
      <LlmList />
      <section v-if="data.memory?.on" class="panel">
        <h2>Mémoire de Tarik</h2>
        <p v-if="data.memory.failures === null" class="panel-bad" role="alert">
          <b>La mémoire ne répond pas.</b> Tarik répond sans elle, et ne retient rien tant qu'elle est arrêtée.</p>
        <p v-else-if="!data.memory.failures.length" class="muted">Aucune opération en échec.</p>
        <template v-else>
          <p role="alert"><b>Des opérations ont échoué ces 7 derniers jours :</b> ce que des clients ont dit n'a pas été
            retenu. Un jeton Claude refusé ou révoqué (<span class="mono">HINDSIGHT_LLM_TOKEN</span>) en est la cause
            la plus courante.</p>
          <div class="table-wrap"><table class="runs">
            <thead><tr><th>Quand</th><th>Cabinet</th><th>Opération</th><th>Erreur</th></tr></thead>
            <tbody><tr v-for="one in data.memory.failures" :key="one.id || one.cabinet + one.at + one.error">
              <td class="num">{{ one.at }}</td><td class="mono">{{ one.cabinet }}</td><td>{{ one.kind }}</td>
              <td>{{ one.error }}</td>
            </tr></tbody>
          </table></div>
        </template>
      </section>
    </template>
  </template>
</template>
