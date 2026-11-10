<!-- A status as a coloured icon: its word (labels.json) in the tooltip and for screen readers. A status without an icon
is shown as its word. -->
<script setup>
import { computed } from "vue";

import { statusName } from "../api.js";
import Icon from "./Icon.vue";

const ICONS = { done: "check", ok: "check", error: "alert", stop: "stop", running: "running", queued: "clock", wait: "clock", question: "help" };
const props = defineProps({ status: { type: String, required: true }, big: Boolean, words: { type: String, default: "" } });
const word = computed(() => props.words || statusName(props.status));
const icon = computed(() => ICONS[props.status]);
</script>

<template>
  <span :class="['chip', 'status-chip', props.status, { big }]" :title="icon ? word : null">
    <template v-if="icon"><Icon :name="icon" :size="big ? 18 : 14" /><span class="sr-only">{{ word }}</span></template>
    <template v-else>{{ word }}</template>
  </span>
</template>
