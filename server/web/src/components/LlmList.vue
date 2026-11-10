<!-- The extra global LLMs, besides Claude and MiniMax (set above): the ones any cabinet may pick on its Réglages page. A
key or a token is never sent here: only whether it is set, and a field to replace it. It saves on its own. -->
<script setup>
import { ref, watch } from "vue";

import { send, usePage } from "../api.js";
import Icon from "./Icon.vue";

const { data, status } = usePage(() => "/settings/llms");
const rows = ref([]);

// The rows as stored, a key empty (it is only replaced); after a refusal they stay as typed.
watch(data, (shown) => {
  if (shown?.error) return;
  rows.value = (shown?.llms || []).map((one) => ({ ...one, key: "" }));
});

const add = () => rows.value.push({ name: "", type: "anthropic-api", model: "", url: "", key: "", key_set: false });
function remove(index) {
  const one = rows.value[index];
  if (one.name && !confirm(`Retirer ${one.name} ? Les cabinets qui l'utilisent passeront sur Claude.`)) return;
  rows.value.splice(index, 1);
}

async function save() {
  const sent = rows.value.map(({ name, type, model, url, key }) => ({ name, type, model, url, key }));
  const answer = await send("/settings/llms", { version: data.value.version, llms: sent });
  if (answer.data) data.value = answer.data;
}
</script>

<template>
  <details v-if="data && status !== 403" class="panel card" open>
    <summary><span class="card-title">LLMs globaux supplémentaires</span></summary>
    <div class="card-body">
      <p v-if="data.unreachable" class="panel-bad" role="alert">
        Les LLMs ne sont pas lisibles : Vault ne répond pas, ou <span class="mono">vault/setup.sh</span> n'a pas été
        relancé depuis cette version.</p>
      <template v-else>
        <p class="muted">Claude et MiniMax sont toujours proposés (leurs réglages sont ci-dessus). Ajoutez ici d'autres LLMs que tout cabinet peut choisir.</p>
        <p v-if="data.error" class="panel-bad" role="alert"><b>Non enregistré :</b> {{ data.error }}.</p>
        <p v-else-if="data.saved" role="status">
          <template v-if="data.saved.length">Enregistré : {{ data.saved.join(", ") }}.</template>
          <template v-else>Rien n'a changé.</template>
          <template v-if="data.fell_back?.length"> Passés sur Claude : {{ data.fell_back.join(", ") }}.</template>
        </p>
        <div v-for="(one, index) in rows" :key="index" class="field-grid llm-row">
          <label class="field"><span class="mono">Nom <span class="req" aria-hidden="true">*</span></span>
            <input v-model="one.name" placeholder="minuscules, chiffres, - et _" :readonly="one.key_set"></label>
          <label class="field"><span class="mono">Type <span class="req" aria-hidden="true">*</span></span>
            <select v-model="one.type"><option v-for="(label, type) in data.types" :key="type" :value="type">{{ label }}</option></select>
          </label>
          <label class="field"><span class="mono">Modèle <span class="req" aria-hidden="true">*</span></span>
            <input v-model="one.model" placeholder="le nom du modèle chez le fournisseur"></label>
          <label v-if="one.type === 'anthropic-api'" class="field"><span class="mono">Adresse <span class="req" aria-hidden="true">*</span></span>
            <input v-model="one.url" placeholder="https://…"></label>
          <label class="field"><span class="mono">Clé ou jeton <span class="req" aria-hidden="true">*</span></span>
            <input v-model="one.key" type="password" autocomplete="new-password"
              :placeholder="one.key_set ? '•••••••• défini : laisser vide pour le garder' : 'la clé de l\'API, ou le jeton de claude setup-token'"></label>
          <button type="button" class="btn btn-icon" title="Retirer" aria-label="Retirer" @click="remove(index)"><Icon name="trash" /></button>
        </div>
        <div class="llm-actions">
          <button type="button" class="btn" @click="add">Ajouter un LLM</button>
          <button type="button" class="btn btn-primary" @click="save">Enregistrer les LLMs</button>
        </div>
      </template>
    </div>
  </details>
</template>

<style scoped>
.llm-row { align-items: end; margin-bottom: var(--space-3); }
.llm-actions { display: flex; gap: var(--space-3); margin-top: var(--space-3); }
</style>
