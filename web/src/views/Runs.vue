<!-- The runs of the cabinet picked in the sidebar, latest first, each with the runs it started under it; read again at
each change of the cabinet's runs. A type picked shows one workflow's. -->
<script setup>
import { useRoute, useRouter } from "vue-router";

import { graphName, session, statusName, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import PageState from "../components/PageState.vue";
import { useLive } from "../live.js";

const route = useRoute();
const router = useRouter();
const { data, status, loading, reload } = usePage((at) =>
  at.query.graph ? `/executions?graph=${encodeURIComponent(at.query.graph)}` : "/executions");
const cabinet = session.cabinets.find((shown) => shown.chosen)?.name;
if (cabinet) useLive(`cabinet=${encodeURIComponent(cabinet)}`, reload);

const pick = (event) => router.push({ query: event.target.value ? { graph: event.target.value } : {} });
</script>

<template>
  <PageState v-if="!data" :status="status" :loading="loading" />
  <template v-else>
    <div class="head">
      <div>
        <h1>Exécutions<template v-if="data.cabinet"> · <CabinetLabel :name="data.cabinet" /></template></h1>
        <p class="muted">Chaque fois que Tarik travaille : une discussion, un traitement de documents, un dossier, des emails. Les plus récentes en premier, mises à jour toutes seules.</p>
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
        <thead><tr><th>Début</th><th>Type</th><th>Discussion</th><th>État</th><th>Durée</th></tr></thead>
        <tbody>
          <tr v-for="run in data.runs" :key="run.run_id" :class="{ child: run.depth }">
            <td class="num">{{ run.when }}</td>
            <td :style="{ '--depth': run.depth }"><template v-if="run.depth"><span class="under" aria-hidden="true">↳</span> <span
              class="sr-only">Lancée par l'exécution au-dessus :</span></template><RouterLink :to="`/ui/executions/${run.run_id}`">{{ graphName(run.graph) }}</RouterLink></td>
            <td><RouterLink class="mono" :to="`/ui/discussion/${encodeURIComponent(run.session_id)}`">{{ run.session_id }}</RouterLink></td>
            <td><span :class="`chip ${run.status}`">{{ statusName(run.status) }}</span></td>
            <td class="num">{{ run.seconds }} s<template v-if="!run.ended"> …</template></td>
          </tr>
          <tr v-if="!data.runs.length"><td colspan="5" class="empty">Aucune exécution pour l'instant. Elles apparaissent ici dès que Tarik travaille.</td></tr>
        </tbody>
      </table>
    </div>
  </template>
</template>
