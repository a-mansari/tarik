<script setup>
import { graphDescription, graphName, usePage } from "../api.js";
import PageState from "../components/PageState.vue";

const { data, status, loading } = usePage(() => "/workflows");
</script>

<template>
  <PageState v-if="!data" :status="status" :loading="loading" />
  <template v-else>
    <div class="head"><div>
      <h1>Workflows</h1>
      <p class="muted">Ce que Tarik sait faire, étape par étape. Ouvrez un workflow pour voir son schéma et ses exécutions.</p>
    </div></div>
    <div class="cards stack">
      <RouterLink v-for="name in data.graphs" :key="name" class="card" :to="`/ui/workflows/${name}`">
        <b>{{ graphName(name) }}</b>
        <span>{{ graphDescription(name) }}</span>
        <span class="muted">{{ data.counts[name] }} étapes</span>
      </RouterLink>
    </div>
  </template>
</template>

<style scoped>
.stack { grid-template-columns: minmax(0, 1fr); }
</style>
