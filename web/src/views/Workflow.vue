<!-- A workflow's canvas, with one of its recent runs over it when one is picked (?run=), and those runs to pick from. -->
<script setup>
import { graphDescription, graphName, statusName, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import CanvasGraph from "../components/CanvasGraph.vue";
import Icon from "../components/Icon.vue";
import PageState from "../components/PageState.vue";

const { data, status, loading } = usePage((route) => {
  const path = `/workflows/${encodeURIComponent(route.params.graph)}`;
  return route.query.run ? `${path}?run=${encodeURIComponent(route.query.run)}` : path;
});
</script>

<template>
  <PageState v-if="!data" :status="status" :loading="loading" />
  <template v-else>
    <nav class="crumbs" aria-label="Fil d'Ariane"><RouterLink to="/ui/workflows"><Icon name="workflows" :size="14" />Workflows</RouterLink><Icon
      name="next" :size="14" /><span aria-current="page">{{ graphName(data.graph) }}</span></nav>
    <div class="head"><div>
      <h1>{{ graphName(data.graph) }}</h1>
      <p class="what">{{ graphDescription(data.graph) }}</p>
      <p v-if="data.run" class="muted">Exécution du {{ data.run.when }}, cabinet <CabinetLabel :name="data.run.cabinet" /> · <span
        :class="`chip ${data.run.status}`">{{ statusName(data.run.status) }}</span> · <RouterLink :to="`/ui/executions/${data.run.run_id}`">toutes ses étapes</RouterLink></p>
      <p v-else class="muted">Le schéma seul. Choisissez une exécution à droite pour la voir dessus.</p>
    </div></div>
    <CanvasGraph :key="data.run?.run_id || ''" :graph="data.graph" :run="data.run?.run_id || ''" />
    <section class="panel">
      <h2>Exécutions récentes</h2>
      <ul class="runs-side">
        <li><RouterLink :to="`/ui/workflows/${data.graph}`" :aria-current="data.run ? null : 'true'"><span>Le schéma seul</span></RouterLink></li>
        <li v-for="item in data.runs" :key="item.run_id"><RouterLink :to="`/ui/workflows/${data.graph}?run=${item.run_id}`"
          :aria-current="data.run?.run_id === item.run_id ? 'true' : null">
          <span class="num">{{ item.when }} · <span class="mono">{{ item.session_id }}</span></span>
          <span :class="`chip ${item.status}`">{{ statusName(item.status) }}</span>
        </RouterLink></li>
        <li v-if="!data.runs.length" class="muted">Pas encore d'exécution.</li>
      </ul>
    </section>
  </template>
</template>
