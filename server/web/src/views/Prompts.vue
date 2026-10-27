<!-- The LLM Calls page: each model call Tarik made for the cabinet picked in the sidebar, latest first, with its
conversation, its run, and its tokens and cost; read again at each change of the cabinet; "Plus ancien" pages back (?before=). -->
<script setup>
import { callLink, pickedCabinet, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import ModelUsage from "../components/ModelUsage.vue";
import PageState from "../components/PageState.vue";
import { useLive } from "../live.js";

const { data, status, loading, reload } = usePage((route) =>
  route.query.before ? `/llm-calls?before=${encodeURIComponent(route.query.before)}` : "/llm-calls");
const cabinet = pickedCabinet();
if (cabinet) useLive(`cabinet=${encodeURIComponent(cabinet)}`, reload);
</script>

<template>
  <PageState v-if="!data" :status="status" :loading="loading" />
  <template v-else>
    <div class="head">
      <div>
        <h1>LLM Calls<template v-if="data.cabinet"> · <CabinetLabel :name="data.cabinet" /></template></h1>
        <p class="muted">Chaque appel du modèle par Tarik, le plus récent d'abord : ce qu'il a reçu (ses instructions, ses outils et toute la discussion), dans quelle discussion et quelle exécution. Gardés 30 jours. Ils contiennent les mots des clients : à ne pas partager.</p>
      </div>
    </div>
    <div class="table-wrap">
      <table class="runs">
        <thead><tr><th>Appel</th><th>Discussion</th><th>Exécution</th><th>Messages reçus</th><th>Modèle</th></tr></thead>
        <tbody>
          <tr v-for="call in data.calls" :key="call.id">
            <td><RouterLink :to="callLink(call.session_id, call.id)">{{ call.when }}</RouterLink></td>
            <td>
              <RouterLink :to="`/ui/discussion/${encodeURIComponent(call.session_id)}`">{{
                data.titles[call.session_id] || "(sans texte)" }}</RouterLink>
            </td>
            <td>
              <RouterLink v-if="call.run_id" :to="`/ui/executions/${call.run_id}`">Discussion avec Tarik</RouterLink>
              <span v-else class="muted">—</span>
            </td>
            <td class="num">{{ call.messages }}</td>
            <td><ModelUsage :usage="data.usage[call.id]" /></td>
          </tr>
          <tr v-if="!data.calls.length"><td colspan="5" class="muted">Aucun appel enregistré pour ce cabinet.</td></tr>
        </tbody>
      </table>
    </div>
    <p v-if="data.older"><RouterLink class="panel-button" :to="`/ui/llm-calls?before=${data.older}`">Plus ancien</RouterLink></p>
  </template>
</template>
