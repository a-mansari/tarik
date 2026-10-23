<!-- A conversation's model calls (of the cabinet picked in the sidebar), in order, each opened to read its whole prompt
(fetched the first time it is opened); the call asked for (?call=) opens at once. New calls are added as they come:
one opened stays open. -->
<script setup>
import { nextTick, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";

import { session, usePage } from "../api.js";
import Icon from "../components/Icon.vue";
import PageState from "../components/PageState.vue";
import PromptBody from "../components/PromptBody.vue";
import { useLive } from "../live.js";

const route = useRoute();
const { data, status, loading, reload } = usePage((at) => `/llm-calls/${encodeURIComponent(at.params.session)}`);
useLive(`session=${encodeURIComponent(route.params.session)}`, reload);
const cabinetShown = (name) => session.cabinets.find((cabinet) => cabinet.name === name)?.display || name;

const prompts = reactive({}), loaded = new Set(); // call id -> its prompt, or what went wrong
async function load(id, event) {
  if (!event.target.open || loaded.has(id)) return;
  loaded.add(id);
  try {
    const answer = await fetch(`/prompts/${id}`);
    if (!answer.ok) throw new Error(answer.status);
    prompts[id] = await answer.json();
  } catch {
    prompts[id] = "Impossible de charger ce prompt. Refermez-le et rouvrez-le pour réessayer.";
    loaded.delete(id);
  }
}

// The call asked for: open (its toggle loads it) and scrolled to, once the list is shown.
const asked = String(route.query.call || "");
const list = ref(null);
const stop = watch(list, async (shown) => {
  if (!shown) return;
  stop();
  await nextTick();
  const opened = document.getElementById(`call-${asked}`);
  if (!opened) return;
  load(Number(asked), { target: opened });
  opened.scrollIntoView({ block: "start" });
});
</script>

<template>
  <PageState v-if="!data" :status="status" :loading="loading" />
  <template v-else>
    <nav class="crumbs" aria-label="Fil d'Ariane"><RouterLink to="/ui/llm-calls"><Icon name="prompts" :size="14" />LLM Calls</RouterLink><Icon
      name="next" :size="14" /><span aria-current="page">{{ cabinetShown(data.cabinet) }}</span></nav>
    <div class="head"><div>
      <h1>{{ data.title || "Discussion" }}</h1>
      <p class="muted"><span>{{ data.calls.length }}</span> appel(s) du modèle, dans l'ordre. Ouvrez-en un pour voir exactement ce que Tarik a reçu : ses instructions, ses outils, puis chaque message.</p>
    </div></div>
    <ol ref="list" class="prompt-calls">
      <li v-for="(call, index) in data.calls" :id="`item-${call.id}`" :key="call.id">
        <details :id="`call-${call.id}`" :open="asked === String(call.id)" @toggle="load(call.id, $event)">
          <summary><span class="time num">{{ call.when }}</span> <b>Appel {{ index + 1 }}</b> <span class="muted">{{ call.agent }} · {{ call.messages }} message(s)</span></summary>
          <div class="prompt">
            <template v-if="typeof prompts[call.id] === 'string'">{{ prompts[call.id] }}</template>
            <PromptBody v-else-if="prompts[call.id]" :prompt="prompts[call.id]" />
            <template v-else>…</template>
          </div>
        </details>
      </li>
    </ol>
  </template>
</template>
