<!-- The runs of the cabinet picked in the sidebar, latest first, each with the runs it started under it and its model calls' tokens and cost (theirs included); read again at
each change of the cabinet's runs. A type picked shows one workflow's. -->
<script setup>
import { useRoute, useRouter } from "vue-router";

import { graphName, pickedCabinet, statusName, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import ModelUsage from "../components/ModelUsage.vue";
import PageState from "../components/PageState.vue";
import { useLive } from "../live.js";

const route = useRoute();
const router = useRouter();
const { data, status, loading, reload } = usePage((at) =>
  at.query.graph ? `/executions?graph=${encodeURIComponent(at.query.graph)}` : "/executions");
const cabinet = pickedCabinet();
const TALKING = new Set(["agent", "routine"]); // the runs whose conversation the Discussion page shows
if (cabinet) useLive(`cabinet=${encodeURIComponent(cabinet)}`, reload);

const pick = (event) => router.push({ query: event.target.value ? { graph: event.target.value } : {} });
</script>

<template>
  <PageState v-if="!data" :status="status" :loading="loading" />
  <template v-else>
    <div class="head">
      <div>
        <h1>Exécutions<template v-if="data.cabinet"> · <CabinetLabel :name="data.cabinet" /></template></h1>
        <p class="muted">Chaque fois que Tarik travaille : une discussion, un traitement de documents, un dossier, des emails, une routine. Les plus récentes en premier, mises à jour toutes seules.</p>
      </div>
      <form class="filters" @submit.prevent>
        <label>Type
          <select name="graph" :value="route.query.graph || ''" @change="pick">
            <option value="">Tous</option>
            <option v-for="name in data.graphs" :key="name" :value="name">{{ graphName(name) }}</option>
          </select>
        </label>
      </form>
    </div>
    <div class="table-wrap">
      <table class="runs">
        <thead><tr><th>Début</th><th>Type</th><th>Discussion</th><th>État</th><th>Durée</th><th>Modèle</th></tr></thead>
        <tbody>
          <tr v-for="run in data.runs" :key="run.run_id" :class="{ child: run.depth }">
            <td class="num">{{ run.when }}</td>
            <td :style="{ '--depth': run.depth }">
              <template v-if="run.depth">
                <span class="under" aria-hidden="true">↳</span> <span class="sr-only">Lancée par l'exécution au-dessus :</span>
              </template>
              <RouterLink :to="`/ui/executions/${run.run_id}`">{{ graphName(run.graph) }}</RouterLink>
            </td>
            <td>
              <!-- Linked where a conversation is shown on the Discussion page: Tarik's own turn (« Discussion avec
              Tarik ») and a routine's period (its report, a reply to it); nothing elsewhere. -->
              <RouterLink v-if="TALKING.has(run.graph)" class="mono"
                :to="`/ui/discussion/${encodeURIComponent(run.session_id)}`">{{ run.session_id }}</RouterLink>
            </td>
            <td><span :class="`chip ${run.status}`">{{ statusName(run.status) }}</span></td>
            <td class="num">{{ run.seconds }} s<template v-if="!run.ended"> …</template></td>
            <td><ModelUsage :usage="data.usage[run.run_id]" /></td>
          </tr>
          <tr v-if="!data.runs.length"><td colspan="6" class="empty">Aucune exécution pour l'instant. Elles apparaissent ici dès que Tarik travaille.</td></tr>
        </tbody>
      </table>
    </div>
  </template>
</template>
