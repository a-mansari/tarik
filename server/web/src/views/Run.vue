<!-- One run step by step: what it was asked, its canvas, what the client reads, its conversation, the model calls it
made, and its steps, each added as it ends while the run goes on; once it has ended, the page is read again. An admin
can stop it while it goes on, with the run it was started from and every run it started. -->
<script setup>
import { computed, onUnmounted, ref, shallowRef, watch } from "vue";

import { callLink, graphName, plural, send, session, statusName, stepName, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import CanvasGraph from "../components/CanvasGraph.vue";
import Icon from "../components/Icon.vue";
import ModelUsage from "../components/ModelUsage.vue";
import PageState from "../components/PageState.vue";
import RunSteps from "../components/RunSteps.vue";
import RunTalk from "../components/RunTalk.vue";
import { followRun, stillGoing } from "../live.js";

const { data, status, loading, reload } = usePage((route) => `/executions/${encodeURIComponent(route.params.run)}`);
const run = computed(() => data.value?.run);
// The client's messages earlier in the conversation, answered by other runs.
const asks = computed(() => (data.value?.earlier || []).filter((entry) => entry.who === "Client").length);

// The run going on, followed once for the steps list and the canvas together; its end reads the page again (its
// outcome: what the client reads, the conversation).
const events = shallowRef(null);
watch(run, (found) => {
  if (events.value || !found || !stillGoing(found.status)) return;
  const last = Math.max(0, ...found.steps.map((step) => step.id));
  events.value = followRun(found.run_id, last).on("end", reload);
}, { immediate: true });
onUnmounted(() => events.value?.close());

// Stopped by an admin: the whole tree goes (a nested run goes on inside its parent's step), what is done stays done.
const stopping = ref(false);
const stopError = ref("");
async function stop() {
  const tree = run.value.parent_id || run.value.children.length
    ? " L'exécution qui l'a lancée et toutes celles qu'elle a lancées s'arrêtent aussi." : "";
  const asked = `Arrêter cette exécution ?${tree} L'étape en cours se termine d'abord ; ce qui est fait dans `
    + "Experio reste fait.";
  if (!confirm(asked)) return;
  stopping.value = true;
  const answer = await send(`/executions/${encodeURIComponent(run.value.run_id)}/stop`, {});
  stopError.value = answer.ok ? "" : answer.data?.error || "Arrêt refusé.";
  if (!answer.ok) stopping.value = false; // stopped: the page follows its end (followRun), the button stays off
}
</script>

<template>
  <PageState v-if="!data" :status="status" :loading="loading" />
  <template v-else>
    <nav class="crumbs" aria-label="Fil d'Ariane">
      <RouterLink to="/ui/executions"><Icon name="runs" :size="14" />Exécutions</RouterLink>
      <Icon name="next" :size="14" />
      <span aria-current="page">{{ graphName(run.graph) }}</span>
    </nav>
    <div class="head">
      <div>
        <h1>{{ graphName(run.graph) }}</h1>
        <p class="muted">Cabinet <b><CabinetLabel :name="run.cabinet" /></b>
          · discussion <span class="mono">{{ run.session_id }}</span>
          · début {{ run.when }}
          · modèle <ModelUsage :usage="data.usage" full /></p>
        <div v-if="run.parent_id || run.children.length" class="run-links">
          <RouterLink v-if="run.parent_id" class="run-link" :to="`/ui/executions/${run.parent_id}`">
            <Icon name="up" />
            <span>
              <small>Lancée par</small>
              <b>{{ graphName(run.parent_graph) }}</b>
              <small v-if="run.parent_node">étape « {{ stepName(run.parent_graph, run.parent_node) }} »</small>
            </span>
          </RouterLink>
          <RouterLink
            v-for="child in run.children"
            :key="child.run_id"
            class="run-link"
            :to="`/ui/executions/${child.run_id}`"
          >
            <Icon name="down" />
            <span><small>A lancé</small><b>{{ graphName(child.graph) }}</b><small>{{ child.when }}</small></span>
            <span :class="`chip ${child.status}`">{{ statusName(child.status) }}</span>
          </RouterLink>
        </div>
      </div>
      <div class="state">
        <span :class="`chip big ${run.status}`">{{ statusName(run.status) }}</span>
        <span class="num muted">{{ run.seconds }} s</span>
        <button v-if="session.admin && stillGoing(run.status)" type="button" class="panel-button" :disabled="stopping"
          @click="stop"><Icon name="stop" :size="14" /> {{ stopping ? "Arrêt demandé…" : "Arrêter" }}</button>
      </div>
    </div>

    <div v-if="stopError" class="panel bad" role="alert"><b>Non arrêtée :</b> {{ stopError }}</div>

    <div v-if="run.error" class="panel bad"><b>L'exécution a échoué.</b> {{ run.error }}</div>

    <CanvasGraph :graph="run.graph" :run="run.run_id" :events="events" />

    <section class="panel">
      <h2>Ce qui a été demandé</h2>
      <dl class="pairs">
        <template v-for="(value, key) in data.asked" :key="key"><dt>{{ key }}</dt><dd>{{ value }}</dd></template>
      </dl>
    </section>

    <section v-if="data.summary" class="panel accent">
      <h2>Ce que le client lit</h2>
      <pre class="said">{{ data.summary }}</pre>
    </section>

    <section v-if="data.conversation.length" class="panel">
      <h2>La discussion</h2>
      <template v-if="data.earlier.length">
        <details class="earlier">
          <summary>Plus tôt dans la discussion : {{ asks }} message{{ plural(asks) }} du client,
            répondu{{ plural(asks) }} par d'autres exécutions</summary>
          <RunTalk :entries="data.earlier" />
        </details>
        <p class="muted">Cette exécution répond au dernier message ; ses appels et ses étapes, ci-dessous, sont les siens.</p>
      </template>
      <RunTalk :entries="data.conversation" />
    </section>

    <section v-if="data.prompts.length" class="panel">
      <h2>Ce que Tarik a reçu</h2>
      <p class="muted">Chaque appel du modèle pendant cette exécution : ses instructions, ses outils et la discussion, tels qu'envoyés.</p>
      <ul class="runs-side">
        <li v-for="(call, index) in data.prompts" :key="call.id">
          <RouterLink :to="callLink(run.session_id, call.id)">
            <span class="num">{{ call.when }} · Appel {{ index + 1 }}</span>
            <span class="muted">{{ call.messages }} message(s) · <ModelUsage :usage="data.call_usage[call.id]" /></span>
          </RouterLink>
        </li>
      </ul>
    </section>

    <section class="panel">
      <h2>Les étapes</h2>
      <p class="muted">Dans l'ordre où elles se sont terminées. Ouvrez une étape pour voir tout ce qu'elle a produit.</p>
      <RunSteps :run="run.run_id" :graph="run.graph" :steps="run.steps" :events="events" />
    </section>
  </template>
</template>
