<!-- A canvas's zoom bar, in its canvas-tools: « Tout voir » and the zoom slider (in %, with its marks). The workflow
canvas (CanvasGraph.vue) and the harness map (HarnessMap.vue) share it, and its range. -->
<script>
export const MIN_ZOOM = 10;
export const MAX_ZOOM = 250;
const TICKS = [10, 25, 50, 75, 100, 150, 200, 250]; // the slider's marks, in %
</script>

<script setup>
import { useId } from "vue";

import Icon from "./Icon.vue";

const zoom = defineModel("zoom", { type: Number, required: true }); // in %
const emit = defineEmits(["fit"]);
const ticks = useId(); // one datalist per bar on a page
</script>

<template>
  <button type="button" class="icon-button" aria-label="Tout voir" data-tip="Tout voir : le schéma entier, centré" @click="emit('fit')">
    <Icon name="fit" />
  </button>
  <label class="zoom-slider" data-tip="Zoom : glisser, ou cliquer une graduation">
    <Icon name="zoom-out" />
    <input type="range" :min="MIN_ZOOM" :max="MAX_ZOOM" step="5" :list="ticks" :value="zoom"
      aria-label="Zoom" :aria-valuetext="`${zoom} %`" @input="zoom = Number($event.target.value)">
    <datalist :id="ticks"><option v-for="tick in TICKS" :key="tick" :value="tick" :label="`${tick} %`"></option></datalist>
    <Icon name="zoom-in" />
    <span class="zoom-value">{{ zoom }} %</span>
  </label>
</template>
