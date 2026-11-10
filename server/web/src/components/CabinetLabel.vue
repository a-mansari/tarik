<!-- A cabinet as the team reads it: its name in Experio (its database name when Experio gave none). -->
<script setup>
import { computed } from "vue";

import { cabinetDisplay } from "../api.js";

const props = defineProps({ name: { type: String, required: true } });
const shown = computed(() => cabinetDisplay(props.name) || "");
</script>

<template>
  <span class="cabinet-name">{{ shown || name }}</span>
</template>
