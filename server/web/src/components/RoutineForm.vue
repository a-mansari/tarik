<!-- One routine's form on the Routines page, for a new routine or one already made, in three steps: what Tarik does,
when, who gets the report; then the routine read back in one sentence before it is saved. -->
<script setup>
import { computed } from "vue";

const props = defineProps({
  form: { type: Object, required: true },
  kinds: { type: Object, required: true }, // the server's names of the tasks (routines.KINDS)
  weekdays: { type: Array, required: true },
  busy: { type: Boolean, default: false },
  submitWords: { type: String, default: "Enregistrer" },
});
defineEmits(["submit", "cancel"]);

// Each task's card: a short title and what it does; the recap and the server say it with kinds[...].
const TASKS = {
  instruction: ["Suivre une consigne", "Vous écrivez ce que Tarik doit faire ; il le fait à chaque fois et vous envoie sa réponse."],
  progress: ["Envoyer l'avancement", "L'état de chaque dossier : pièces, relevés bancaires, opérations en suspens, déclarations."],
  tva: ["Préparer la TVA", "La déclaration de la période terminée, pour chaque dossier. Une déclaration déjà commencée n'est jamais touchée."],
  ras: ["Préparer les RAS", "Les déclarations de retenue à la source de la période terminée, préparées comme la TVA."],
};
const EXAMPLES = [
  "Dis-moi les factures restées en brouillon et ce qui les bloque.",
  "Liste les opérations bancaires en suspens de plus de 30 jours.",
  "Signale les dossiers sans relevé bancaire le mois dernier.",
];
const RHYTHMS = { day: "Chaque jour", week: "Chaque semaine", month: "Chaque mois" };
const HOURS = Array.from({ length: 24 }, (_, hour) => hour);
const MONTH_DAYS = Array.from({ length: 28 }, (_, day) => day + 1);
const PROMPT_MOST = 2000; // the server's PROMPT_CHARACTERS

function setRhythm(every) {
  props.form.every = every;
  if (every === "week" && props.form.day > 6) props.form.day = 0;
  if (every === "month" && props.form.day < 1) props.form.day = 1;
}

// As the server says the schedule (routines.when) and refuses midnight (routine_problem): kept the same.
const when = computed(() => {
  const { every, day, hour } = props.form;
  if (every === "day") return `chaque jour à ${hour} h`;
  if (every === "week") return `chaque ${props.weekdays[day]} à ${hour} h`;
  return `chaque mois, le ${day} à ${hour} h`;
});
const midnight = computed(() => props.form.hour === 0 && props.form.kind !== "progress");
const what = computed(() => (props.form.kind === "instruction" && props.form.prompt.trim()
  ? `suivre la consigne « ${props.form.prompt.trim()} »`
  : props.kinds[props.form.kind]));
</script>

<template>
  <form class="routine-form" @submit.prevent="$emit('submit')">
    <fieldset>
      <legend><span class="step">1</span> Que doit faire Tarik ?</legend>
      <div class="tasks">
        <label v-for="(task, key) in TASKS" :key="key" class="task" :class="{ chosen: form.kind === key }">
          <input v-model="form.kind" type="radio" name="kind" :value="key">
          <b>{{ task[0] }}</b>
          <span class="muted">{{ task[1] }}</span>
        </label>
      </div>
      <div v-if="form.kind === 'instruction'" class="field">
        <label for="routine-prompt">Consigne</label>
        <textarea id="routine-prompt" v-model="form.prompt" rows="3" :maxlength="PROMPT_MOST" required
          placeholder="Écrivez ce que Tarik doit faire à chaque fois."></textarea>
        <span class="count">{{ form.prompt.length }} / {{ PROMPT_MOST }}</span>
        <div class="examples">
          <span class="muted">Exemples :</span>
          <button v-for="example in EXAMPLES" :key="example" type="button" class="panel-button"
            @click="form.prompt = example">{{ example }}</button>
        </div>
      </div>
    </fieldset>

    <fieldset>
      <legend><span class="step">2</span> Quand ?</legend>
      <div class="choices" aria-label="Rythme">
        <button v-for="(words, key) in RHYTHMS" :key="key" type="button" class="panel-button"
          :aria-pressed="form.every === key" @click="setRhythm(key)">{{ words }}</button>
      </div>
      <div class="when">
        <div v-if="form.every === 'week'" class="choices" aria-label="Jour de la semaine">
          <button v-for="(name, index) in weekdays" :key="name" type="button" class="panel-button day"
            :aria-pressed="form.day === index" :aria-label="name" @click="form.day = index">{{ name.slice(0, 3) }}</button>
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
      <p v-if="midnight" class="warn" role="alert">À minuit, le serveur est encore la veille : choisissez au moins 1 h.</p>
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
      <p><b>Tarik va {{ what }}</b>, {{ when }}, et envoyer le rapport à <b>{{ form.notify || "…" }}</b>.</p>
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
.task { position: relative; display: grid; gap: .25rem; padding: .7rem .8rem; border: 1px solid var(--line); border-radius: 8px; cursor: pointer; background: var(--bg); color: var(--fg); }
.task input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.task.chosen { border-color: var(--accent); background: var(--accent-soft); }
.task:focus-within { outline: 2px solid var(--accent); outline-offset: 2px; }
.task span { font-size: .85rem; }
.examples { display: flex; flex-wrap: wrap; align-items: center; gap: .4rem; }
.choices { display: flex; flex-wrap: wrap; gap: .3rem; }
.day { text-transform: capitalize; }
.when { display: flex; flex-wrap: wrap; align-items: center; gap: .8rem; }
.field.inline { display: flex; align-items: center; gap: .5rem; }
.field.inline select { width: auto; }
.warn { color: var(--bad); margin: 0; font-size: .85rem; }
.recap { border-left: 4px solid var(--accent); background: var(--bg); border-radius: 6px; padding: .7rem .9rem; display: grid; gap: .35rem; }
.recap p { margin: 0; }
</style>
