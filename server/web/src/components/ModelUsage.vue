<!-- Model calls' tokens and cost (server/api/runs.py usage_shown): every token and the cost, the input and output on
hover, or written out with "full"; "—" before any call. -->
<script setup>
defineProps({ usage: { type: Object, default: null }, full: Boolean });
</script>

<template>
  <span v-if="usage" class="num" :title="full ? undefined : usage.detail">{{ usage.tokens }} tokens<template v-if="full">
    ({{ usage.detail }})</template> · {{ usage.cost }}</span>
  <span v-else class="muted">—</span>
</template>
