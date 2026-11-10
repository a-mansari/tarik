<!-- The runs of the cabinet picked in the sidebar, latest first, each with the runs it started under it and its model calls' tokens and cost (theirs included); read again at
each change of the cabinet's runs. A type picked shows one workflow's. -->
<script setup>
import { onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { graphName, pickedCabinet, plural, send, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import ModelUsage from "../components/ModelUsage.vue";
import PageState from "../components/PageState.vue";
import { useLive } from "../live.js";
import StatusChip from "../components/StatusChip.vue";
import Icon from "../components/Icon.vue";

const route = useRoute();
const router = useRouter();
const { data, status, loading, reload } = usePage((at) =>
  at.query.graph ? `/executions?graph=${encodeURIComponent(at.query.graph)}` : "/executions");
const cabinet = pickedCabinet();
const TALKING = new Set(["agent", "routine"]); // the runs whose conversation the Discussion page shows
if (cabinet) useLive(`cabinet=${encodeURIComponent(cabinet)}`, reload);

// The unread bubble's popup: opens on a click, closes on a click elsewhere or Escape.
const open = ref(false);
const bubble = ref(null);
const close = (event) => {
  if (event.type === "keydown" ? event.key === "Escape" : !bubble.value?.contains(event.target)) open.value = false;
};
onMounted(() => {
  document.addEventListener("click", close);
  document.addEventListener("keydown", close);
});
onBeforeUnmount(() => {
  document.removeEventListener("click", close);
  document.removeEventListener("keydown", close);
});
const markRead = async () => {
  await send("/executions-lues");
  open.value = false;
  reload();
};
const pick = (event) => router.push({ query: event.target.value ? { graph: event.target.value } : {} });
</script>

<template>
  <PageState v-if="!data" :status="status" :loading="loading" />
  <template v-else>
    <div class="head">
      <div>
        <h1>Exécutions<template v-if="data.cabinet"> · <CabinetLabel :name="data.cabinet" /></template>
          <span v-if="data.unread" ref="bubble" class="unread">
            <button type="button" class="unread-count" :aria-expanded="open" aria-haspopup="true"
              :aria-label="`${data.unread} exécution${plural(data.unread)} non lue${plural(data.unread)}`"
              @click="open = !open">{{ data.unread }}</button>
            <div v-if="open" class="unread-popup" role="dialog" aria-label="Exécutions non lues">
              <p class="unread-title"><b>{{ data.unread }}</b> exécution{{ plural(data.unread) }} non lue{{ plural(data.unread) }}</p>
              <p class="muted">Une exécution est lue dès que vous l'ouvrez.</p>
              <button type="button" class="primary icon-button" title="Tout marquer comme lu" aria-label="Tout marquer comme lu" @click="markRead"><Icon name="check-all" /></button>
            </div>
          </span>
        </h1>
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
              Tarik ») and a routine's period (its report, a reply to it); a dash elsewhere. -->
              <RouterLink v-if="TALKING.has(run.graph)" class="mono"
                :to="`/ui/discussion/${encodeURIComponent(run.session_id)}`">{{ run.session_id }}</RouterLink>
              <span v-else class="muted" aria-label="Aucune discussion">—</span>
            </td>
            <td><StatusChip :status="run.status" /></td>
            <td class="num">{{ run.seconds }} s<template v-if="!run.ended"> …</template></td>
            <td><ModelUsage :usage="data.usage[run.run_id]" /></td>
          </tr>
          <tr v-if="!data.runs.length"><td colspan="6" class="empty">Aucune exécution pour l'instant. Elles apparaissent ici dès que Tarik travaille.</td></tr>
        </tbody>
      </table>
    </div>
  </template>
</template>
