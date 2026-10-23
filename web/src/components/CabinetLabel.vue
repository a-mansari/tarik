<!-- A cabinet as the team reads it: its name in Experio, its database beside it (what the code and the logs use). -->
<script setup>
import { computed } from "vue";

import { session } from "../api.js";

const props = defineProps({ name: { type: String, required: true } });
const shown = computed(() => session.cabinets.find((cabinet) => cabinet.name === props.name)?.display || "");
</script>

<template>
  <span class="cabinet-name">
    <template v-if="shown">{{ shown }} <span class="mono muted">({{ name }})</span></template>
    <span v-else class="mono">{{ name }}</span>
  </span>
</template>
