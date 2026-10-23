<!-- A run's steps, in the order they ended; each opened loads all it produced. While the run goes on (``events``, the
run page's followRun shared with its canvas) each new step is added as it ends. -->
<script setup>
import { reactive, ref, watch } from "vue";

import { statusName, stepName } from "../api.js";

const props = defineProps({
  run: { type: String, required: true },
  graph: { type: String, required: true },
  steps: { type: Array, required: true },
  events: { type: Object, default: null },
});

const list = ref([]);
watch(() => props.steps, (steps) => { list.value = [...steps]; }, { immediate: true }); // read again: the server's
watch(() => props.events, (events) => {
  events?.on("step", (step) => {
    if (!list.value.some((shown) => shown.id === step.id)) list.value.push({ ...step, arrived: true });
  });
}, { immediate: true });

const depth = (step) => (step.path ? step.path.split("|").length : 0);

// A step's whole output, fetched the first time it is opened.
const details = reactive({}), loaded = new Set();
async function load(step, event) {
  if (!event.target.open || loaded.has(step)) return;
  loaded.add(step);
  try {
    const answer = await fetch(`/runs/${props.run}/steps/${step}`);
    details[step] = JSON.stringify(await answer.json(), null, 2);
  } catch {
    details[step] = "Impossible de charger le détail de cette étape. Rouvrez-la pour réessayer.";
    loaded.delete(step);
  }
}
</script>

<template>
  <ol class="steps">
    <li v-for="step in list" :key="step.id" :class="{ new: step.arrived }" :style="{ '--depth': depth(step) }">
      <details @toggle="load(step.id, $event)">
        <summary>
          <span class="time num">{{ step.when }}</span>
          <span class="name">{{ stepName(graph, step.node) }}</span>
          <span v-if="step.status" :class="`chip ${step.status}`">{{ statusName(step.status) }}</span>
          <span class="line">{{ step.summary }}</span>
        </summary>
        <pre class="detail">{{ details[step.id] ?? "…" }}</pre>
      </details>
    </li>
    <li v-if="!list.length" class="empty">Pas encore d'étape.</li>
  </ol>
</template>
