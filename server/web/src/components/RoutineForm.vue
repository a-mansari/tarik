<!-- One routine's form on the Routines page, for a new routine or one already made, in three steps: what Tarik does,
when, who gets the report; then the routine read back in one sentence before it is saved. -->
<script setup>
import { computed } from "vue";

const props = defineProps({
  form: { type: Object, required: true },
  weekdays: { type: Array, required: true },
  busy: { type: Boolean, default: false },
  submitWords: { type: String, default: "Enregistrer" },
});
defineEmits(["submit", "cancel"]);

// Each task as the client reads it: its name and what it does, in a sentence.
const TASKS = {
  instruction: {
    name: "Suivre une consigne",
    text: "Vous écrivez ce que Tarik doit faire ; il le fait à chaque fois et vous envoie sa réponse.",
  },
  progress: {
    name: "Envoyer l'avancement",
    text: "L'état de chaque dossier : pièces, relevés bancaires, opérations en suspens, déclarations.",
  },
  tva: {
    name: "Préparer la TVA",
    text: "La déclaration de la période terminée, pour chaque dossier. Une déclaration déjà commencée n'est jamais touchée.",
  },
  ras: {
    name: "Préparer les RAS",
    text: "Les déclarations de retenue à la source de la période terminée, préparées comme la TVA.",
  },
};
const EXAMPLES = [
  "Dis-moi les factures restées en brouillon et ce qui les bloque.",
  "Liste les opérations bancaires en suspens de plus de 30 jours.",
  "Signale les dossiers sans relevé bancaire le mois dernier.",
];
const RHYTHMS = { day: "Chaque jour", week: "Chaque semaine", month: "Chaque mois" };
const HOURS = Array.from({ length: 24 }, (_, hour) => hour);
const MONTH_DAYS = Array.from({ length: 28 }, (_, day) => day + 1);
const PROMPT_MOST = 2000;

function setRhythm(every) {
  props.form.every = every;
  if (every === "week" && props.form.day > 6) props.form.day = 0;
  if (every === "month" && props.form.day < 1) props.form.day = 1;
}

// The schedule and the whole routine read back, as the server will say them.
const when = computed(() => {
  const { every, day, hour } = props.form;
  if (every === "day") return `chaque jour à ${hour} h`;
  if (every === "week") return `chaque ${props.weekdays[day]} à ${hour} h`;
  return `chaque mois, le ${day} à ${hour} h`;
});
const midnight = computed(() => props.form.hour === 0 && props.form.kind !== "progress");
const what = computed(() => (props.form.kind === "instruction" && props.form.prompt.trim()
  ? `« ${props.form.prompt.trim()} »`
  : TASKS[props.form.kind].name.toLowerCase()));
</script>

<template>
  <form class="routine-form" @submit.prevent="$emit('submit')">
    <fieldset>
      <legend><span class="step">1</span> Que doit faire Tarik ?</legend>
      <div class="tasks" role="radiogroup">
        <label v-for="(task, key) in TASKS" :key="key" class="task" :class="{ chosen: form.kind === key }">
          <input v-model="form.kind" type="radio" :value="key">
          <b>{{ task.name }}</b>
          <span class="muted">{{ task.text }}</span>
        </label>
      </div>
      <label v-if="form.kind === 'instruction'" class="field">
        <span>Consigne</span>
        <textarea v-model="form.prompt" rows="3" :maxlength="PROMPT_MOST" required
          placeholder="Écrivez ce que Tarik doit faire à chaque fois."></textarea>
        <span class="examples">
          <span class="muted">Exemples :</span>
          <button v-for="example in EXAMPLES" :key="example" type="button" class="example"
            @click="form.prompt = example">{{ example }}</button>
          <span class="muted count">{{ form.prompt.length }} / {{ PROMPT_MOST }}</span>
        </span>
      </label>
    </fieldset>

    <fieldset>
      <legend><span class="step">2</span> Quand ?</legend>
      <div class="segmented" role="radiogroup" aria-label="Rythme">
        <button v-for="(words, key) in RHYTHMS" :key="key" type="button" role="radio"
          :aria-checked="form.every === key" :class="{ on: form.every === key }" @click="setRhythm(key)">{{ words }}</button>
      </div>
      <div class="when">
        <div v-if="form.every === 'week'" class="days" role="radiogroup" aria-label="Jour">
          <button v-for="(name, index) in weekdays" :key="name" type="button" role="radio"
            :aria-checked="form.day === index" :class="{ on: form.day === index }" @click="form.day = index">{{ name.slice(0, 3) }}</button>
        </div>
        <label v-if="form.every === 'month'" class="field inline"><span>Le</span>
          <select v-model.number="form.day">
            <option v-for="day in MONTH_DAYS" :key="day" :value="day">{{ day }}</option>
          </select></label>
        <label class="field inline"><span>à</span>
          <select v-model.number="form.hour">
            <option v-for="hour in HOURS" :key="hour" :value="hour">{{ hour }} h</option>
          </select><span class="muted">heure de Casablanca</span></label>
      </div>
      <p v-if="midnight" class="warn">À minuit, le serveur est encore la veille : choisissez au moins 1 h.</p>
    </fieldset>

    <fieldset>
      <legend><span class="step">3</span> À qui envoyer le rapport ?</legend>
      <label class="field">
        <span>Adresse e-mail</span>
        <input v-model="form.notify" type="email" required autocomplete="off" placeholder="manager@cabinet.ma">
        <small class="muted">Un collaborateur actif d'Experio, dans les adresses autorisées du cabinet (Réglages).</small>
      </label>
    </fieldset>

    <div class="recap" aria-live="polite">
      <p><b>Tarik va {{ what }}</b>, {{ when }}, et envoyer le rapport à
        <b>{{ form.notify || "…" }}</b>.</p>
      <p class="muted">Sur tous les dossiers que voit le compte Experio de Tarik. Rien n'est jamais déclaré,
        comptabilisé ni confirmé par une routine.</p>
      <label class="switch"><input v-model="form.enabled" type="checkbox"> Active dès maintenant</label>
    </div>

    <div class="panel-actions">
      <button type="submit" class="primary" :disabled="busy || midnight">{{ busy ? "Enregistrement…" : submitWords }}</button>
      <button type="button" class="panel-button" @click="$emit('cancel')">Annuler</button>
    </div>
  </form>
</template>

<style scoped>
.routine-form { display: grid; gap: 1.1rem; }
fieldset { border: 0; margin: 0; padding: 0; display: grid; gap: .6rem; }
legend { font-weight: 600; margin-bottom: .5rem; display: flex; align-items: center; gap: .5rem; }
.step { display: inline-grid; place-items: center; width: 1.5rem; height: 1.5rem; border-radius: 50%; background: var(--accent-soft); color: var(--accent); font-size: .8rem; }
.tasks { display: grid; grid-template-columns: repeat(auto-fill, minmax(14rem, 1fr)); gap: .6rem; }
.task { display: grid; gap: .25rem; padding: .7rem .8rem; border: 1px solid var(--line); border-radius: 8px; cursor: pointer; background: var(--bg); }
.task input { position: absolute; opacity: 0; pointer-events: none; }
.task.chosen { border-color: var(--accent); background: var(--accent-soft); }
.task:focus-within { outline: 2px solid var(--accent); outline-offset: 2px; }
.task span { font-size: .85rem; }
.examples { display: flex; flex-wrap: wrap; align-items: center; gap: .4rem; }
.example { font: inherit; font-size: .8rem; border: 1px dashed var(--line); background: var(--bg); color: var(--fg); border-radius: 999px; padding: .2rem .6rem; cursor: pointer; }
.example:hover { border-color: var(--accent); }
.count { margin-left: auto; }
.segmented { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; width: fit-content; }
.segmented button, .days button { font: inherit; border: 0; background: var(--bg); color: var(--fg); padding: .45rem .9rem; cursor: pointer; }
.segmented button + button { border-left: 1px solid var(--line); }
.segmented .on, .days .on { background: var(--accent); color: var(--on-accent); }
.when { display: flex; flex-wrap: wrap; align-items: center; gap: .8rem; }
.days { display: flex; gap: .3rem; }
.days button { border: 1px solid var(--line); border-radius: 6px; padding: .35rem .55rem; text-transform: capitalize; }
.field.inline { display: flex; align-items: center; gap: .5rem; }
.field.inline select { width: auto; }
.warn { color: var(--bad); margin: 0; font-size: .85rem; }
.recap { border-left: 4px solid var(--accent); background: var(--bg); border-radius: 6px; padding: .7rem .9rem; display: grid; gap: .35rem; }
.recap p { margin: 0; }
.switch { display: flex; align-items: center; gap: .4rem; }
</style>
