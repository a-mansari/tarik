<!-- The Logs serveur page, an admin's: who did what in the UI, latest first (app/audit.py), one action or one person
at a time, paged back. -->
<script setup>
import { useRoute, useRouter } from "vue-router";

import { graphName, roleName, usePage } from "../api.js";
import PageState from "../components/PageState.vue";

const route = useRoute();
const router = useRouter();
const { data, status, loading } = usePage((r) => `/logs-serveur?${new URLSearchParams(r.query)}`);

// A filter picked: the page again with it (and from the latest).
function filter(name, value) {
  const query = { ...route.query, [name]: value };
  delete query.before;
  for (const key of Object.keys(query)) if (!query[key]) delete query[key];
  router.push({ path: "/ui/logs-serveur", query });
}

// The page before: older entries, the same filters kept.
function older() {
  const query = { before: data.value.older };
  if (data.value.action) query.action = data.value.action;
  if (data.value.actor) query.actor = data.value.actor;
  return { path: "/ui/logs-serveur", query };
}
</script>

<template>
  <PageState v-if="!data || status === 403" :status="status" :loading="loading" />
  <template v-else>
    <div class="head">
      <div>
        <h1>Logs serveur</h1>
        <p class="muted">Qui a fait quoi dans ces pages : connexions, comptes, contenu, réglages des cabinets, dispositions des schémas, exécutions lancées à la main. Le plus récent d'abord. Aucun mot de passe ni lien n'y figure.</p>
      </div>
      <form class="filters inline" @submit.prevent>
        <label>Action
          <select name="action" :value="data.action || ''" @change="filter('action', $event.target.value)">
            <option value="">Toutes</option>
            <option v-for="[name, words] in data.actions" :key="name" :value="name">{{ words }}</option>
          </select>
        </label>
        <label>Qui
          <select name="actor" :value="data.actor" @change="filter('actor', $event.target.value)">
            <option value="">Tout le monde</option>
            <option v-for="name in data.actors" :key="name" :value="name">{{ name }}</option>
          </select>
        </label>
      </form>
    </div>
    <div class="table-wrap">
      <table class="runs journal">
        <thead><tr><th>Quand</th><th>Qui</th><th>Quoi</th><th>Sur</th><th>Détail</th></tr></thead>
        <tbody>
          <tr v-for="e in data.entries" :key="e.id">
            <td class="num">{{ e.at }}</td>
            <td class="mono">{{ e.actor }}</td>
            <td><span class="chip" :class="e.action === 'login_failed' ? 'error' : ''">{{ data.words[e.action] || e.action }}</span></td>
            <td class="mono">
              <RouterLink v-if="e.action === 'run_started' && e.detail.run_id" :to="`/ui/executions/${e.detail.run_id}`">{{ graphName(e.target) }}</RouterLink>
              <template v-else-if="e.action === 'layout_saved' || e.action === 'layout_reset'">{{ graphName(e.target) }}</template>
              <template v-else>{{ e.target }}</template>
            </td>
            <td>
              <template v-if="e.detail.keys?.length">{{ e.detail.keys.join(", ") }}</template>
              <template v-if="e.detail.role">{{ roleName(e.detail.role) }}</template>
              <template v-if="e.detail.note"> « {{ e.detail.note }} »</template>
            </td>
          </tr>
          <tr v-if="!data.entries.length"><td colspan="5" class="muted">Rien pour l'instant.</td></tr>
        </tbody>
      </table>
    </div>
    <p v-if="data.older"><RouterLink class="panel-button" :to="older()">Plus ancien</RouterLink></p>
  </template>
</template>
