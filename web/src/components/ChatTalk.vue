<!-- A conversation as n8n's chat shows one: the client's messages with their files, each of Tarik's turns with the
tools it called (done, failed, or still going) before its reply, and Tarik typing while its run goes on. Words only as
the server's markdown (*_html). Patched in place at each refresh: a tools list left open stays open. -->
<script setup>
import ChatAvatar from "./ChatAvatar.vue";
import Icon from "./Icon.vue";

const props = defineProps({
  session: { type: String, required: true },
  messages: { type: Array, default: () => [] },
  latest: { type: Object, default: null },
  going: { type: Boolean, default: false },
});

const fileLink = (name) =>
  `/ui/api/discussion/${encodeURIComponent(props.session)}/fichier?nom=${encodeURIComponent(name)}`;
const running = (entry) => entry.tools.filter((tool) => tool.state === "running");
const plural = (count) => (count > 1 ? "s" : "");
const mark = { done: "✓", failed: "✕" };
const stateWords = { done: "terminé", failed: "en échec" };
</script>

<template>
  <div id="talk">
    <ol class="talk bubbles">
      <template v-for="(entry, index) in messages" :key="index">
        <li v-if="entry.who === 'Client'" class="client">
          <span class="who"><ChatAvatar kind="client" />Client</span>
          <div v-if="entry.text_html" class="reply" v-html="entry.text_html" />
          <ul v-if="entry.files?.length" class="files">
            <li v-for="name in entry.files" :key="name"><Icon name="file" :size="14" /><span>{{ name }}</span><a
              class="download" :href="fileLink(name)" download :title="`Télécharger ${name}`"
              :aria-label="`Télécharger ${name}`"><Icon name="download" :size="14" /></a></li>
          </ul>
          <ul v-if="entry.skipped?.length" class="files skipped" aria-label="Fichiers laissés de côté">
            <li v-for="why in entry.skipped" :key="why"><Icon name="file" :size="14" /><span>{{ why }}</span></li>
          </ul>
        </li>
        <li v-else class="tarik">
          <span class="who"><ChatAvatar kind="tarik" />Tarik</span>
          <details v-if="entry.tools.length" class="used">
            <summary>
              <template v-if="running(entry).length"><span class="spin" aria-hidden="true"></span> <span
                class="mono">{{ running(entry).at(-1).name }}</span> en cours…</template>
              <template v-else>{{ entry.tools.length }} outil{{ plural(entry.tools.length) }} utilisé{{ plural(entry.tools.length) }}</template>
            </summary>
            <ul>
              <li v-for="(tool, at) in entry.tools" :key="at" :class="tool.state"><span class="mark"
                aria-hidden="true">{{ mark[tool.state] || "" }}</span>
                <span class="mono">{{ tool.name }}</span>
                <span class="muted">{{ stateWords[tool.state] || "en cours…" }}</span></li>
            </ul>
          </details>
          <div v-if="entry.text_html" class="reply" v-html="entry.text_html" />
          <p v-if="going && index === messages.length - 1" class="typing"><span class="dots" aria-label="Tarik répond"><i></i><i></i><i></i></span>
            <RouterLink class="muted" :to="`/ui/executions/${latest.run_id}`">suivre l'exécution</RouterLink></p>
        </li>
      </template>
      <li v-if="going && (!messages.length || messages.at(-1).who === 'Client')" class="tarik">
        <span class="who"><ChatAvatar kind="tarik" />Tarik</span>
        <p class="typing"><span class="dots" aria-label="Tarik répond"><i></i><i></i><i></i></span>
          <RouterLink class="muted" :to="`/ui/executions/${latest.run_id}`">suivre l'exécution</RouterLink></p>
      </li>
    </ol>
    <p v-if="latest?.status === 'error'" class="bad-text">Le dernier message a échoué : <RouterLink
      :to="`/ui/executions/${latest.run_id}`">voir l'exécution</RouterLink>. Renvoyez-le.</p>
  </div>
</template>
