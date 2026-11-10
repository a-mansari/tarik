<!-- One cabinet setting as a form field, by its kind: simulation or real writes, the model, a secret (replaced, never
shown), or text. The field's value is the parent's (v-model). -->
<script setup>
defineProps({
  f: { type: Object, required: true },
  adapters: { type: Object, default: () => ({}) },
  placeholder: { type: String, default: "" },
});
const value = defineModel({ type: String, default: "" });
</script>

<template>
  <label class="field">
    <span>{{ f.name }} <span class="mono muted">{{ f.key }}</span><template
      v-if="f.optional"> <span class="muted">(facultatif)</span></template></span>
    <select v-if="f.kind === 'flag'" v-model="value" :name="f.key">
      <option value="1">Simulation</option>
      <option value="0">Écritures réelles</option>
    </select>
    <select v-else-if="f.kind === 'adapter'" v-model="value" :name="f.key">
      <option v-for="(label, key) in adapters" :key="key" :value="key">{{ label }}</option>
    </select>
    <input
      v-else-if="f.kind === 'secret'"
      v-model="value"
      type="password"
      :name="f.key"
      autocomplete="new-password"
      :placeholder="placeholder"
    >
    <input
      v-else
      v-model="value"
      :name="f.key"
      :inputmode="f.kind === 'integer' ? 'numeric' : undefined"
    >
  </label>
</template>
