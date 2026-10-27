<!-- One routine's fields on the Routines page, for a routine already made or a new one: what it does, when, who gets
its report, which dossiers. A dossier no longer among the cabinet's open ones stays listed, to be taken off. -->
<script setup>
import { computed } from "vue";

const props = defineProps({
  form: { type: Object, required: true },
  kinds: { type: Object, required: true },
  weekdays: { type: Array, required: true },
  dossiers: { type: Array, required: true }, // the cabinet's open dossiers, [{id, name}]
  names: { type: Map, required: true }, // their names by id
});

const EVERY = { day: "Chaque jour", week: "Chaque semaine", month: "Chaque mois" };
const HOURS = Array.from({ length: 24 }, (_, hour) => hour);
const MONTH_DAYS = Array.from({ length: 28 }, (_, day) => day + 1);

// The cabinet's dossiers, and this routine's own that are no longer among them (closed since).
const options = computed(() => [
  ...props.dossiers,
  ...props.form.dossier_ids.filter((id) => !props.names.has(id)).map((id) => ({ id, name: `n° ${id} (fermé)` })),
]);
const chosen = computed(() => props.form.dossier_ids.map((id) => props.names.get(id) || `n° ${id}`).join(", "));

// A schedule's day as the rhythm needs it: a weekday from Monday (0), a day of the month (1 to 28), none daily.
function rhythmChanged() {
  if (props.form.every === "week" && props.form.day > 6) props.form.day = 0;
  if (props.form.every === "month" && props.form.day < 1) props.form.day = 1;
}
</script>

<template>
  <fieldset class="routine-fields">
    <label class="check"><input v-model="form.enabled" type="checkbox"> Active</label>
    <label class="field"><span>Tâche</span>
      <select v-model="form.kind">
        <option v-for="(words, key) in kinds" :key="key" :value="key">{{ words }}</option>
      </select></label>
    <label class="field"><span>Rythme</span>
      <select v-model="form.every" @change="rhythmChanged">
        <option v-for="(words, key) in EVERY" :key="key" :value="key">{{ words }}</option>
      </select></label>
    <label v-if="form.every === 'week'" class="field"><span>Jour</span>
      <select v-model.number="form.day">
        <option v-for="(name, index) in weekdays" :key="name" :value="index">{{ name }}</option>
      </select></label>
    <label v-if="form.every === 'month'" class="field"><span>Jour du mois</span>
      <select v-model.number="form.day">
        <option v-for="day in MONTH_DAYS" :key="day" :value="day">{{ day }}</option>
      </select></label>
    <label class="field"><span>Heure (Casablanca)</span>
      <select v-model.number="form.hour">
        <option v-for="hour in HOURS" :key="hour" :value="hour">{{ hour }} h</option>
      </select></label>
    <label class="field wide"><span>Rapport envoyé à (un collaborateur qui a ces dossiers dans Experio)</span>
      <input v-model="form.notify" type="email" autocomplete="off" placeholder="manager@cabinet.ma"></label>
    <label v-if="form.kind === 'instruction'" class="field wide"><span>Consigne donnée à Tarik à chaque fois</span>
      <textarea v-model="form.prompt" rows="4" maxlength="2000"
        placeholder="Dis-moi les factures restées en brouillon et ce qui les bloque."></textarea></label>
    <label class="field wide"><span>Dossiers (les seuls que la routine touche)</span>
      <select v-model="form.dossier_ids" multiple size="6">
        <option v-for="dossier in options" :key="dossier.id" :value="dossier.id">{{ dossier.name }}</option>
      </select>
      <small class="muted">{{ chosen || "Aucun" }}</small></label>
  </fieldset>
</template>

<style scoped>
.routine-fields { border: 0; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(12rem, 1fr)); gap: .7rem 1rem; }
.routine-fields .wide { grid-column: 1 / -1; }
.check { display: flex; align-items: center; gap: .4rem; align-self: end; }
</style>
