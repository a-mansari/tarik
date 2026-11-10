<!-- One setting as a form field, by its kind: simulation or real writes, an on/off switch, a choice (an LLM, a type), a secret (replaced, never shown), or
text. Its label is the variable's name, a red star marks a required one, and what it does is its placeholder (and its
tooltip, for a list). The field's value is the parent's (v-model). Used by the cabinet pages and the general one. -->
<script setup>
import { computed } from "vue";

import Icon from "./Icon.vue";

const props = defineProps({
  f: { type: Object, required: true },
});
const value = defineModel({ type: String, default: "" });

// A secret already set is never shown: its placeholder says so, and that an empty field keeps it.
const hint = computed(() => (props.f.kind === "secret" && props.f.value
  ? "•••••••• défini : laisser vide pour le garder"
  : props.f.description));
</script>

<template>
  <label class="field" :title="f.description || undefined">
    <span class="mono">{{ f.key }}<template v-if="!f.optional"><span class="req" aria-hidden="true"> *</span><span
      class="sr-only"> (obligatoire)</span></template></span>
    <span v-if="f.kind === 'toggle'" class="switch">
      <input type="checkbox" role="switch" :name="f.key" :checked="value !== '0'"
        @change="value = $event.target.checked ? '1' : '0'">
      <Icon name="power" :size="18" /><span>{{ value === "0" ? "Désactivée" : "Activée" }}</span>
    </span>
    <select v-else-if="f.kind === 'flag'" v-model="value" :name="f.key">
      <option value="1">Simulation</option>
      <option value="0">Écritures réelles</option>
    </select>
    <select v-else-if="f.kind === 'choice'" v-model="value" :name="f.key">
      <option v-for="(label, key) in f.choices" :key="key" :value="key">{{ label }}</option>
    </select>
    <input
      v-else-if="f.kind === 'secret'"
      v-model="value"
      type="password"
      :name="f.key"
      autocomplete="new-password"
      :placeholder="hint"
    >
    <input
      v-else
      v-model="value"
      :name="f.key"
      :inputmode="f.kind === 'integer' ? 'numeric' : undefined"
      :placeholder="hint"
    >
    <small v-if="f.kind === 'toggle' && f.description" class="field-hint">{{ f.description }}</small>
  </label>
</template>
