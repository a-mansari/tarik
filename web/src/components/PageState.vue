<!-- What a page shows instead of its data: refused (403), not found (404), or still loading. -->
<script setup>
import { computed } from "vue";
import { useRoute } from "vue-router";

defineProps({ status: { type: Number, default: 0 }, loading: { type: Boolean, default: false } });
const route = useRoute();
const isRun = computed(() => route.path.startsWith("/ui/executions/"));
</script>

<template>
  <div v-if="status === 403" class="panel" role="alert">
    <h1>Réservé aux administrateurs</h1>
    <p>Cette page change ce que Tarik utilise pour toute l'équipe. Demandez à un administrateur, ou qu'il vous donne ce rôle.</p>
    <p><RouterLink to="/ui/executions">Retour aux exécutions</RouterLink></p>
  </div>
  <template v-else-if="status === 404 && !isRun">
    <h1>Introuvable</h1>
    <p>Cette page n'existe pas, ou plus. <RouterLink to="/ui/accueil">Revenir à l'accueil</RouterLink>.</p>
  </template>
  <template v-else-if="status === 404">
    <h1>Exécution introuvable</h1>
    <p>Elle a peut-être été lancée sur un autre serveur. <RouterLink to="/ui/executions">Revenir aux exécutions</RouterLink>.</p>
  </template>
  <p v-else-if="loading" class="muted" role="status">Chargement…</p>
</template>
