<!-- The Paramètres page: what every cabinet shares (the mail server, the model, the Experio key, Gladden, Sparrow), read only:
these settings are the server's .env, changed there and applied by a restart. A key or a token is never sent here, only
whether it is set. -->
<script setup>
import LlmList from "../components/LlmList.vue";
import PageState from "../components/PageState.vue";
import SettingField from "../components/SettingField.vue";
import { usePage } from "../api.js";

const { data, status, loading } = usePage(() => "/settings/general");
</script>

<template>
  <PageState v-if="!data || status === 403" :status="status" :loading="loading" />
  <template v-else>
    <div class="head">
      <h1>Paramètres</h1>
    </div>
    <div class="panel panel-accent" role="status">Ces réglages sont ceux du fichier <span class="mono">.env</span> du
      serveur : pour en changer un, modifier ce fichier puis redémarrer Tarik.</div>
      <div class="content-form">
        <details v-for="[title, items] in data.sections" :key="title" class="panel card" open>
          <summary>
            <span class="card-title">{{ title }}</span>
          </summary>
          <div class="card-body"><div class="field-grid">
            <SettingField v-for="f in items" :key="f.key" :model-value="f.kind === 'secret' ? '' : f.value" :f="f" locked />
          </div></div>
        </details>
      </div>
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
