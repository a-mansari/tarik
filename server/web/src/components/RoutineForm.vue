<!-- One routine's form on the Routines page, for a new routine or one already made, in three steps: what Tarik does,
when (one or several weekdays, or a day of the month), who gets the report (one of the cabinet's admins in Experio);
then the routine read back in one sentence before it is saved. -->
<script setup>
import { computed, ref } from "vue";

const props = defineProps({
  form: { type: Object, required: true },
  kinds: { type: Object, required: true }, // the server's names of the tasks (routines.KINDS)
  weekdays: { type: Array, required: true },
  managers: { type: Array, default: null }, // the cabinet's admins in Experio [{name, email}]; null: Experio silent
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
const RHYTHMS = { day: "Chaque jour", week: "Certains jours de la semaine", month: "Chaque mois" };
const WORKDAYS = [0, 1, 2, 3, 4];
const WEEKEND = [5, 6];
const PRESETS = [["En semaine", WORKDAYS], ["Le week-end", WEEKEND]];
const HOURS = Array.from({ length: 24 }, (_, hour) => hour);
const MONTH_DAYS = Array.from({ length: 28 }, (_, day) => day + 1);
const PROMPT_MOST = 2000; // the server's PROMPT_CHARACTERS
const OTHER = "";

const same = (days, wanted) => days.length === wanted.length && wanted.every((day) => days.includes(day));

// A new rhythm starts from its usual days: the working week, the 1st of the month.
function setRhythm(every) {
  if (every !== props.form.every) props.form.days = { day: [], week: [...WORKDAYS], month: [1] }[every];
  props.form.every = every;
}
function toggleDay(day) {
  const days = props.form.days.includes(day) ? props.form.days.filter((d) => d !== day) : [...props.form.days, day];
  props.form.days = days.sort((a, b) => a - b);
}
const monthDay = computed({
  get: () => props.form.days[0] || 1,
  set: (day) => { props.form.days = [day]; },
});

// Who gets the report: one of the cabinet's admins, or another address typed (Experio's staff, a list Experio
// could not give). A routine whose address is no longer among them keeps it, shown as typed.
const listed = computed(() => (props.managers || []).some((manager) => manager.email === props.form.notify));
const typing = ref(!props.managers?.length || (Boolean(props.form.notify) && !listed.value));
const chosenManager = computed({
  get: () => (typing.value ? OTHER : props.form.notify),
  set: (email) => {
    typing.value = email === OTHER;
    props.form.notify = email;
  },
});

// As the server says the schedule (routines.when) and refuses midnight (routine_problem): kept the same.
const when = computed(() => {
  const { every, days, hour } = props.form;
  if (every === "day" || (every === "week" && days.length === 7)) return `chaque jour à ${hour} h`;
  if (every === "week" && same(days, WORKDAYS)) return `en semaine (du lundi au vendredi) à ${hour} h`;
  if (every === "week" && same(days, WEEKEND)) return `le week-end à ${hour} h`;
  if (every === "week") {
    const names = [...days].sort((a, b) => a - b).map((day) => props.weekdays[day]);
    if (!names.length) return "…";
    const named = names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} et ${names.at(-1)}`;
    return `chaque ${named} à ${hour} h`;
  }
  return `chaque mois, le ${days[0]} à ${hour} h`;
});
const midnight = computed(() => props.form.hour === 0 && props.form.kind !== "progress");
const noDay = computed(() => props.form.every === "week" && !props.form.days.length);
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
      <div v-if="form.every === 'week'" class="week">
        <div class="choices" aria-label="Raccourcis">
          <button v-for="[words, days] in PRESETS" :key="words" type="button" class="panel-button preset"
            :aria-pressed="same(form.days, days)" @click="form.days = [...days]">{{ words }}</button>
        </div>
        <div class="choices" aria-label="Jours de la semaine (plusieurs possibles)">
          <button v-for="(name, index) in weekdays" :key="name" type="button" class="panel-button day"
            :aria-pressed="form.days.includes(index)" :aria-label="name" @click="toggleDay(index)">{{ name.slice(0, 3) }}</button>
        </div>
      </div>
      <div class="when">
        <label v-if="form.every === 'month'" class="field inline"><span>Le</span>
          <select v-model.number="monthDay">
            <option v-for="day in MONTH_DAYS" :key="day" :value="day">{{ day }}</option>
          </select></label>
        <label class="field inline"><span>à</span>
          <select v-model.number="form.hour">
            <option v-for="hour in HOURS" :key="hour" :value="hour">{{ hour }} h</option>
          </select><span class="muted">heure de Casablanca</span></label>
      </div>
      <p v-if="noDay" class="warn" role="alert">Choisissez au moins un jour.</p>
      <p v-if="midnight" class="warn" role="alert">À minuit, le serveur est encore la veille : choisissez au moins 1 h.</p>
    </fieldset>

    <fieldset>
      <legend><span class="step">3</span> À qui envoyer le rapport ?</legend>
      <label v-if="managers?.length" class="field">
        <span>Un administrateur du cabinet dans Experio</span>
        <select v-model="chosenManager">
          <option v-for="manager in managers" :key="manager.email" :value="manager.email">{{ manager.name }} — {{ manager.email }}</option>
          <option :value="OTHER">Autre adresse (équipe Experio)…</option>
        </select>
      </label>
      <label v-if="typing || !managers?.length" class="field">
        <span>Adresse e-mail</span>
        <input v-model="form.notify" type="email" required autocomplete="off" placeholder="manager@cabinet.ma">
        <small class="muted"><template v-if="managers === null">Experio n'a pas donné la liste de ses administrateurs :
          tapez l'adresse. </template>Un administrateur actif du cabinet dans Experio, ou de l'équipe Experio, dans les
          adresses autorisées du cabinet (Réglages).</small>
      </label>
    </fieldset>

    <div class="recap" aria-live="polite">
      <p><b>Tarik va {{ what }}</b>, {{ when }}, et envoyer le rapport à <b>{{ form.notify || "…" }}</b>.</p>
      <p class="muted">Sur tous les dossiers que voit le compte Experio de Tarik. Rien n'est jamais déclaré,
        comptabilisé ni confirmé par une routine.</p>
      <label class="switch"><input v-model="form.enabled" type="checkbox"> Active dès maintenant</label>
    </div>

    <div class="panel-actions">
      <button type="submit" class="primary" :disabled="busy || midnight || noDay">{{ busy ? "Enregistrement…" : submitWords }}</button>
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
.task:has(input:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
.task span { font-size: .85rem; }
.examples { display: flex; flex-wrap: wrap; align-items: center; gap: .4rem; }
.choices { display: flex; flex-wrap: wrap; gap: .3rem; }
.week { display: grid; gap: .4rem; }
.day { text-transform: capitalize; min-width: 3.2rem; }
.when { display: flex; flex-wrap: wrap; align-items: center; gap: .8rem; }
.field.inline { display: flex; align-items: center; gap: .5rem; }
.field.inline select { width: auto; }
.warn { color: var(--bad); margin: 0; font-size: .85rem; }
.recap { border-left: 4px solid var(--accent); background: var(--bg); border-radius: 6px; padding: .7rem .9rem; display: grid; gap: .35rem; }
.recap p { margin: 0; }
</style>
