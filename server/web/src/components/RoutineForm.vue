<!-- One routine's form on the Routines page, for a new routine or one already made, in three steps: what Tarik does,
when (one or several weekdays, or a day of the month), who gets the report (one of the cabinet's admins in Experio);
then the routine read back in one sentence before it is saved. Each step: its title on the left, its choices on the right. -->
<script setup>
import { computed, ref } from "vue";

const props = defineProps({
  form: { type: Object, required: true },
  kinds: { type: Object, required: true }, // the server's names of the tasks (routines.KINDS)
  weekdays: { type: Array, required: true },
  presets: { type: Object, required: true }, // the server's working week and weekend, {workdays, weekend}
  managers: { type: Array, default: null }, // the cabinet's admins in Experio [{name, email}]; null: Experio silent
  busy: { type: Boolean, default: false },
  submitWords: { type: String, default: "Enregistrer" },
});
defineEmits(["submit", "cancel"]);

// Each task's card: a short title and what it does; the recap and the server say it with kinds[...].
const TASKS = {
  instruction: ["Suivre une consigne", "Ce que vous écrivez, fait à chaque fois."],
  progress: ["Envoyer l'avancement", "Pièces, relevés, opérations en suspens, déclarations."],
  tva: ["Préparer la TVA", "Période terminée ; une déclaration commencée n'est pas touchée."],
  ras: ["Préparer les RAS", "Retenues à la source de la période terminée."],
};
const EXAMPLES = [
  "Dis-moi les factures restées en brouillon et ce qui les bloque.",
  "Liste les opérations bancaires en suspens de plus de 30 jours.",
  "Signale les dossiers sans relevé bancaire le mois dernier.",
];
const RHYTHMS = { day: "Chaque jour", week: "Certains jours", month: "Chaque mois" };
const PRESETS = computed(() => [["En semaine", props.presets.workdays], ["Le week-end", props.presets.weekend]]);
const MONTH_DAYS = Array.from({ length: 28 }, (_, day) => day + 1);
const PROMPT_MOST = 2000; // the server's PROMPT_CHARACTERS
const OTHER = "other"; // the « another address » option, never an address

const same = (days, wanted) => days.length === wanted.length && wanted.every((day) => days.includes(day));

// The routine as the form opened on it: its own days and address come back when chosen again.
const opened = { every: props.form.every, days: [...props.form.days], notify: props.form.notify };

// A new rhythm starts from the routine's own days for its rhythm, else its usual ones: the working week, the 1st.
function setRhythm(every) {
  if (every !== props.form.every) {
    const usual = { day: [], week: [...props.presets.workdays], month: [1] }[every];
    props.form.days = every === opened.every ? [...opened.days] : usual;
  }
  props.form.every = every;
}
function toggleDay(day) {
  const days = props.form.days.includes(day) ? props.form.days.filter((d) => d !== day) : [...props.form.days, day];
  props.form.days = days.sort((a, b) => a - b);
}
// The time as the time field writes it, "08:30", kept as the routine's hour and minute.
const two = (number) => String(number).padStart(2, "0");
const time = computed({
  get: () => `${two(props.form.hour)}:${two(props.form.minute)}`,
  set: (value) => {
    if (!value) return; // a field emptied keeps the last time
    [props.form.hour, props.form.minute] = value.split(":").map(Number);
  },
});
const monthDay = computed({
  get: () => props.form.days[0],
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
    const ownTyped = opened.notify && !(props.managers || []).some((manager) => manager.email === opened.notify);
    props.form.notify = email === OTHER ? (ownTyped ? opened.notify : "") : email;
  },
});

// As the server says the schedule (routines.when) and refuses midnight (routine_problem): kept the same.
const when = computed(() => {
  const { every, days, hour, minute } = props.form;
  const at = minute ? `${hour} h ${two(minute)}` : `${hour} h`;
  if (every === "day" || (every === "week" && days.length === 7)) return `chaque jour à ${at}`;
  if (every === "week" && same(days, props.presets.workdays)) return `en semaine (du lundi au vendredi) à ${at}`;
  if (every === "week" && same(days, props.presets.weekend)) return `le week-end à ${at}`;
  if (every === "week") {
    const names = days.map((day) => props.weekdays[day]); // kept in order: toggleDay sorts them
    if (!names.length) return "…";
    const named = names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} et ${names.at(-1)}`;
    return `chaque ${named} à ${at}`;
  }
  return `chaque mois, le ${days[0]} à ${at}`;
});
const midnight = computed(() => props.form.hour === 0 && props.form.kind !== "progress");
const noDay = computed(() => props.form.every === "week" && !props.form.days.length);
const what = computed(() => (props.form.kind === "instruction" && props.form.prompt.trim()
  ? `suivre la consigne « ${props.form.prompt.trim()} »`
  : props.kinds[props.form.kind]));
</script>

<template>
  <form class="routine-form" @submit.prevent="$emit('submit')">
    <fieldset class="step-row">
      <legend><span class="step">1</span> Que doit faire Tarik&nbsp;?</legend>
      <div class="controls">
        <div class="tasks">
          <label v-for="(task, key) in TASKS" :key="key" class="task" :class="{ chosen: form.kind === key }">
            <input v-model="form.kind" type="radio" name="kind" :value="key">
            <b>{{ task[0] }}</b>
            <span class="muted">{{ task[1] }}</span>
          </label>
        </div>
        <div v-if="form.kind === 'instruction'" class="field">
          <label for="routine-prompt" class="sr-only">Consigne</label>
          <textarea id="routine-prompt" v-model="form.prompt" rows="3" :maxlength="PROMPT_MOST" required
            placeholder="Écrivez ce que Tarik doit faire à chaque fois."></textarea>
          <span class="count">{{ form.prompt.length }} / {{ PROMPT_MOST }}</span>
          <div class="examples">
            <span class="muted">Exemples :</span>
            <button v-for="example in EXAMPLES" :key="example" type="button" class="example"
              @click="form.prompt = example">{{ example }}</button>
          </div>
        </div>
      </div>
    </fieldset>

    <fieldset class="step-row">
      <legend><span class="step">2</span> Quand&nbsp;?</legend>
      <div class="controls">
        <div class="segmented" role="group" aria-label="Rythme">
          <button v-for="(words, key) in RHYTHMS" :key="key" type="button"
            :aria-pressed="form.every === key" @click="setRhythm(key)">{{ words }}</button>
        </div>
        <div v-if="form.every === 'week'" class="week">
          <div class="segmented days" role="group" aria-label="Jours de la semaine (plusieurs possibles)">
            <button v-for="(name, index) in weekdays" :key="name" type="button" :aria-pressed="form.days.includes(index)"
              :aria-label="name" @click="toggleDay(index)">{{ name.slice(0, 3) }}</button>
          </div>
          <div class="presets" role="group" aria-label="Raccourcis">
            <button v-for="[words, days] in PRESETS" :key="words" type="button" class="preset"
              :class="{ on: same(form.days, days) }" @click="form.days = [...days]">{{ words }}</button>
          </div>
        </div>
        <div class="when">
          <label v-if="form.every === 'month'" class="field inline"><span>Le</span>
            <select v-model.number="monthDay">
              <option v-for="day in MONTH_DAYS" :key="day" :value="day">{{ day }}</option>
            </select></label>
          <label class="field inline"><span>À</span>
            <input v-model="time" type="time" step="60" required><span class="muted">heure de Casablanca</span></label>
        </div>
        <p v-if="noDay" class="warn" role="alert">Choisissez au moins un jour.</p>
        <p v-if="midnight" class="warn" role="alert">Entre minuit et 1 h, le serveur est encore la veille : choisissez au moins 1 h.</p>
      </div>
    </fieldset>

    <fieldset class="step-row">
      <legend><span class="step">3</span> À qui envoyer le rapport&nbsp;?</legend>
      <div class="controls">
        <label v-if="managers?.length" class="field">
          <span class="sr-only">Un administrateur du cabinet dans Experio</span>
          <select v-model="chosenManager" required>
            <option value="" disabled>Choisissez un administrateur du cabinet…</option>
            <option v-for="manager in managers" :key="manager.email" :value="manager.email">{{ manager.name }} — {{ manager.email }}</option>
            <option :value="OTHER">Autre adresse (équipe Experio)…</option>
          </select>
        </label>
        <label v-if="typing || !managers?.length" class="field">
          <span class="sr-only">Adresse e-mail</span>
          <input v-model="form.notify" type="email" required autocomplete="off" placeholder="manager@cabinet.ma">
          <small class="muted"><template v-if="managers === null">Experio n'a pas donné ses administrateurs : tapez
            l'adresse. </template>Un administrateur actif du cabinet, ou un membre de l'équipe Experio, parmi les adresses
            autorisées (Réglages).</small>
        </label>
      </div>
    </fieldset>

    <footer class="form-foot">
      <p class="recap" aria-live="polite">Tarik va <b>{{ what }}</b>, {{ when }}, et envoyer le rapport à
        <b>{{ form.notify || "…" }}</b>.</p>
      <div class="foot-actions">
        <label class="switch"><input v-model="form.enabled" type="checkbox" role="switch"> Active dès maintenant</label>
        <button type="button" class="panel-button" @click="$emit('cancel')">Annuler</button>
        <button type="submit" class="primary" :disabled="busy || midnight || noDay">{{ busy ? "Enregistrement…" : submitWords }}</button>
      </div>
    </footer>
  </form>
</template>

<style scoped>
.routine-form { display: grid; }
.step-row { border: 0; border-top: 1px solid var(--line); margin: 0; padding: 1rem 0; display: grid; grid-template-columns: 11rem minmax(0, 1fr); gap: .6rem 1.2rem; }
.step-row:first-child { border-top: 0; padding-top: .2rem; }
legend { float: left; font-weight: 600; display: flex; align-items: flex-start; gap: .5rem; padding: 0; line-height: 1.5rem; }
.step { flex: none; display: inline-grid; place-items: center; width: 1.5rem; height: 1.5rem; border-radius: 50%; background: var(--accent-soft); color: var(--accent); font-size: .8rem; }
.controls { display: grid; gap: .7rem; min-width: 0; }
.tasks { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .5rem; }
.task { position: relative; display: grid; align-content: start; gap: .2rem; padding: .6rem .7rem; border: 1px solid var(--line); border-radius: 8px; cursor: pointer; background: var(--bg); color: var(--fg); }
.task input { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.task:hover { border-color: var(--muted); }
.task.chosen { border-color: var(--accent); background: var(--accent-soft); box-shadow: inset 0 0 0 1px var(--accent); }
.task:has(input:focus-visible) { outline: 2px solid var(--accent); outline-offset: 2px; }
.task span { font-size: .8rem; line-height: 1.35; }
.examples { display: flex; flex-wrap: wrap; align-items: center; gap: .35rem; font-size: .8rem; }
.example, .preset { font: inherit; background: none; border-radius: 999px; padding: .2rem .6rem; cursor: pointer; }
.example:focus-visible, .preset:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.example { color: var(--fg); border: 1px dashed var(--line); }
.example:hover { border-color: var(--accent); }
.segmented { display: inline-flex; flex-wrap: wrap; justify-self: start; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: var(--bg); }
.segmented button { font: inherit; font-size: .85rem; color: var(--fg); background: none; border: 0; border-left: 1px solid var(--line); padding: .4rem .85rem; cursor: pointer; }
.segmented button:first-child { border-left: 0; }
.segmented button:hover { background: var(--accent-soft); }
.segmented button[aria-pressed="true"] { background: var(--accent); color: var(--on-accent); }
.segmented button:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.days button { text-transform: capitalize; min-width: 3rem; }
.week { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem .8rem; }
.presets { display: flex; gap: .3rem; }
.preset { font-size: .8rem; color: var(--muted); border: 1px solid transparent; }
.preset:hover { color: var(--fg); }
.preset.on { color: var(--accent); border-color: var(--accent); }
.when { display: flex; flex-wrap: wrap; align-items: center; gap: .8rem; }
.field.inline { display: flex; align-items: center; gap: .5rem; }
.field.inline :is(select, input) { width: auto; }
.warn { color: var(--bad); margin: 0; font-size: .85rem; }
.form-foot { border-top: 1px solid var(--line); padding-top: 1rem; display: grid; gap: .8rem; }
.recap { margin: 0; padding: .6rem .8rem; border-radius: 6px; background: var(--bg); border-left: 3px solid var(--accent); }
.foot-actions { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: .5rem; }
.foot-actions .switch { margin-right: auto; }
@media (max-width: 720px) {
  .step-row, .tasks { grid-template-columns: 1fr; }
  /* phone: the joined bar wraps, so each button gets its own border */
  .segmented { border: 0; border-radius: 0; gap: .3rem; background: none; }
  .segmented button, .segmented button:first-child { border: 1px solid var(--line); border-radius: 6px; background: var(--bg); }
}
</style>
