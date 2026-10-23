<!-- A run's conversation as bubbles: who said what (Tarik's words rendered by the server: text_html), the tools Tarik
called and what they answered, its thinking on demand. -->
<script setup>
import RunAvatar from "./RunAvatar.vue";

defineProps({ entries: { type: Array, required: true } });
const kind = (entry) => (entry.who === "Client" ? "client" : entry.tool ? "tool" : "tarik");
</script>

<template>
  <ol class="talk bubbles">
    <li v-for="(entry, index) in entries" :key="index" :class="kind(entry)">
      <span class="who"><RunAvatar :kind="kind(entry)" />{{ entry.who }}</span>
      <details v-if="entry.thinking" class="thinking"><summary>Sa réflexion</summary><pre>{{ entry.thinking }}</pre></details>
      <ul v-if="entry.calls?.length" class="calls">
        <li v-for="(call, at) in entry.calls" :key="at">Utilise <b>{{ call.name }}</b> <span class="mono">{{ call.args }}</span></li>
      </ul>
      <template v-if="entry.text">
        <details v-if="entry.tool"><summary>Réponse de l'outil</summary><pre>{{ entry.text }}</pre></details>
        <div v-else class="reply" v-html="entry.text_html"></div>
      </template>
    </li>
  </ol>
</template>
