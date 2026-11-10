<!-- The Paramètres page: what every cabinet shares (the mail server, the model, the Experio key, Gladden, Sparrow), in
plain words, each change kept (who, when). A key or a token is never sent here: only whether it is set, and a field to
replace it. -->
<script setup>
import { computed, ref, watch } from "vue";

import { plural, send, usePage } from "../api.js";
import PageState from "../components/PageState.vue";

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
const placeholder = (f) => (f.value ? "Défini — laisser vide pour le garder" : "Non défini");
</script>

<template>
  <PageState v-if="!data || status === 403" :status="status" :loading="loading" />
  <template v-else>
    <div class="head"><div>
      <h1>Paramètres</h1>
      <p class="what">Ce que Tarik partage entre tous les cabinets. Une clé ou un jeton n'est jamais affiché : il se remplace seulement. Un changement s'applique dès le message suivant.</p>
    </div></div>
    <div v-if="data.unreachable" class="panel bad" role="alert">
      <b>Paramètres illisibles.</b> Vault ne répond pas, ou le rôle de cette page (tarik-settings) n'a pas le droit de
      lire ces réglages : relancer <span class="mono">vault/setup.sh</span>.</div>
    <template v-else>
      <div v-if="data.error" class="panel bad" role="alert"><b>Non enregistré :</b> {{ data.error }}.</div>
      <div v-if="data.saved" class="panel accent" role="status">
        <template v-if="data.saved.length">Enregistré : {{ savedNames }}. En service dès maintenant.</template>
        <template v-else>Rien n'a changé.</template>
      </div>
      <div class="flow content-flow">
        <form id="general-form" class="content-form" autocomplete="off" @submit.prevent="save">
          <details v-for="[title, items] in data.sections" :key="title" class="panel card" open>
            <summary>
              <span class="card-title">{{ title }}</span>
              <span class="card-meta"><span class="muted">{{ items.length }} réglage{{ plural(items.length) }}</span></span>
            </summary>
            <div class="card-body">
              <label v-for="f in items" :key="f.key" class="field">
                <span>{{ f.name }} <span class="mono muted">{{ f.key }}</span><template
                  v-if="f.optional"> <span class="muted">(facultatif)</span></template></span>
                <input
                  v-if="f.kind === 'secret'"
                  v-model="form[f.key]"
                  type="password"
                  :name="f.key"
                  autocomplete="new-password"
                  :placeholder="placeholder(f)"
                >
                <input v-else v-model="form[f.key]" :name="f.key" :inputmode="f.kind === 'integer' ? 'numeric' : undefined">
                <small class="muted">{{ f.description }}</small>
              </label>
            </div>
          </details>
        </form>
        <div class="side-column">
          <section class="panel save-card" aria-label="Enregistrer">
            <button type="submit" form="general-form" class="primary">Enregistrer</button>
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
