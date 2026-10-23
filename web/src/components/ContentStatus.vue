<!-- What an import came to, told on the page (refused: nothing was saved); after a saved import the page reads its
data again. -->
<script setup>
import { nextTick, ref } from "vue";

const emit = defineEmits(["saved"]);
const told = ref(null), box = ref(null);

async function tell(result) {
  told.value = result;
  await nextTick();
  box.value?.scrollIntoView({ block: "center" });
  if (result.saved) setTimeout(() => { told.value = null; emit("saved"); }, 900);
}
defineExpose({ tell });
</script>

<template>
  <div v-if="told" ref="box" :class="['panel', told.bad ? 'bad' : 'accent']" role="status">{{ told.text }}</div>
</template>
