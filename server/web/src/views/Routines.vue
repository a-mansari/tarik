<!-- The Routines page: the routines of the cabinet picked in the sidebar, only that cabinet's. Each one turned on or
off, rescheduled, its instruction, address and dossiers changed, or deleted; a new one made. The server checks each
change as Tarik's own routine tool does (server/api/routines.py), and keeps it in the Logs serveur. -->
<script setup>
import { ref, watch } from "vue";

import { send, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import PageState from "../components/PageState.vue";

const { data, status, loading } = usePage(() => "/routines");
const forms = ref({}); // routine id (or "new") -> its form
const EVERY = { day: "Chaque jour", week: "Chaque semaine", month: "Chaque mois" };
const HOURS = Array.from({ length: 24 }, (_, hour) => hour);
const MONTH_DAYS = Array.from({ length: 28 }, (_, day) => day + 1);

const blank = () => ({
  enabled: true, kind: "instruction", dossier_ids: [], every: "month", day: 5, hour: 8, notify: "", prompt: "",
});
const formOf = (routine) => ({
  id: routine.id, enabled: routine.enabled, kind: routine.kind, dossier_ids: [...routine.dossier_ids],
  every: routine.every, day: routine.day, hour: routine.hour, notify: routine.notify, prompt: routine.prompt,
});

watch(data, (shown) => {
  const next = { new: forms.value.new && shown?.error ? forms.value.new : blank() };
  for (const routine of shown?.routines || []) next[routine.id] = formOf(routine);
  forms.value = next;
});

// A schedule's day as the rhythm needs it: a weekday from Monday (0), a day of the month (1 to 28), none daily.
function rhythmChanged(form) {
  if (form.every === "week" && form.day > 6) form.day = 0;
  if (form.every === "month" && form.day < 1) form.day = 1;
}

async function act(action, form) {
  if (action === "delete" && !confirm("Supprimer cette routine ? Ses rapports passés sont oubliés.")) return;
  const answer = await send("/routines", { cabinet: data.value.cabinet, action, routine: form });
  if (answer.data) data.value = answer.data;
  if (answer.ok) scrollTo({ top: 0 });
}

const dossierName = (id) => data.value.dossiers.find((dossier) => dossier.id === id)?.name || `n° ${id}`;
</script>

<template>
  <PageState v-if="!data || status === 403 || status === 404" :status="status" :loading="loading" />
  <template v-else>
    <div class="head"><div>
      <h1>Routines · <CabinetLabel :name="data.cabinet" /></h1>
      <p class="what">Ce que Tarik refait à date fixe pour ce cabinet, et dont il envoie le rapport par e-mail. Une
        routine prépare et rapporte : elle ne déclare, ne comptabilise et ne confirme jamais rien. Ses dossiers sont
        revérifiés à chaque fois : l'adresse du rapport doit encore les avoir dans Experio.</p>
    </div></div>
    <div v-if="data.error" class="panel bad" role="alert"><b>Non enregistré :</b> {{ data.error }}.</div>

    <form v-for="routine in data.routines" :key="routine.id" class="panel routine" @submit.prevent="act('save', forms[routine.id])">
      <template v-if="forms[routine.id]">
        <div class="routine-head">
          <h2>n° {{ routine.id }} · {{ data.kinds[routine.kind] }}</h2>
          <span class="chip" :class="routine.enabled ? 'ok' : ''">{{ routine.enabled ? "Active" : "En pause" }}</span>
        </div>
        <p class="muted">{{ routine.schedule }}, prochaine le <span class="num">{{ routine.next_run_at }}</span> · créée par
          {{ routine.created_by }}<template v-if="routine.last_due"> · dernière le
            <span class="num">{{ routine.last_due }}</span>{{ routine.last_report === null ? " (en cours)" : "" }}</template></p>
        <details v-if="routine.last_report" class="last"><summary>Dernier rapport</summary><pre>{{ routine.last_report }}</pre></details>
        <fieldset class="routine-fields">
          <label class="check"><input v-model="forms[routine.id].enabled" type="checkbox"> Active</label>
          <label class="field"><span>Tâche</span>
            <select v-model="forms[routine.id].kind">
              <option v-for="(words, key) in data.kinds" :key="key" :value="key">{{ words }}</option>
            </select></label>
          <label class="field"><span>Rythme</span>
            <select v-model="forms[routine.id].every" @change="rhythmChanged(forms[routine.id])">
              <option v-for="(words, key) in EVERY" :key="key" :value="key">{{ words }}</option>
            </select></label>
          <label v-if="forms[routine.id].every === 'week'" class="field"><span>Jour</span>
            <select v-model.number="forms[routine.id].day">
              <option v-for="(name, index) in data.weekdays" :key="name" :value="index">{{ name }}</option>
            </select></label>
          <label v-if="forms[routine.id].every === 'month'" class="field"><span>Jour du mois</span>
            <select v-model.number="forms[routine.id].day">
              <option v-for="day in MONTH_DAYS" :key="day" :value="day">{{ day }}</option>
            </select></label>
          <label class="field"><span>Heure (Casablanca)</span>
            <select v-model.number="forms[routine.id].hour">
              <option v-for="hour in HOURS" :key="hour" :value="hour">{{ hour }} h</option>
            </select></label>
          <label class="field wide"><span>Rapport envoyé à</span>
            <input v-model="forms[routine.id].notify" type="email" autocomplete="off"></label>
          <label v-if="forms[routine.id].kind === 'instruction'" class="field wide"><span>Consigne donnée à Tarik à
            chaque fois</span>
            <textarea v-model="forms[routine.id].prompt" rows="4" maxlength="2000"></textarea></label>
          <label class="field wide"><span>Dossiers<template v-if="forms[routine.id].kind === 'instruction'">
            (facultatif)</template></span>
            <select v-model="forms[routine.id].dossier_ids" multiple size="6">
              <option v-for="dossier in data.dossiers" :key="dossier.id" :value="dossier.id">{{ dossier.name }}</option>
            </select>
            <small class="muted">{{ forms[routine.id].dossier_ids.map(dossierName).join(", ") || "Aucun" }}</small></label>
        </fieldset>
        <div class="actions">
          <button type="submit" class="primary">Enregistrer</button>
          <button type="button" class="danger" @click="act('delete', forms[routine.id])">Supprimer</button>
        </div>
      </template>
    </form>
    <p v-if="!data.routines.length" class="muted">Aucune routine pour ce cabinet.</p>

    <form v-if="forms.new" class="panel routine accent" @submit.prevent="act('create', forms.new)">
      <h2>Nouvelle routine</h2>
      <fieldset class="routine-fields">
        <label class="check"><input v-model="forms.new.enabled" type="checkbox"> Active</label>
        <label class="field"><span>Tâche</span>
          <select v-model="forms.new.kind">
            <option v-for="(words, key) in data.kinds" :key="key" :value="key">{{ words }}</option>
          </select></label>
        <label class="field"><span>Rythme</span>
          <select v-model="forms.new.every" @change="rhythmChanged(forms.new)">
            <option v-for="(words, key) in EVERY" :key="key" :value="key">{{ words }}</option>
          </select></label>
        <label v-if="forms.new.every === 'week'" class="field"><span>Jour</span>
          <select v-model.number="forms.new.day">
            <option v-for="(name, index) in data.weekdays" :key="name" :value="index">{{ name }}</option>
          </select></label>
        <label v-if="forms.new.every === 'month'" class="field"><span>Jour du mois</span>
          <select v-model.number="forms.new.day">
            <option v-for="day in MONTH_DAYS" :key="day" :value="day">{{ day }}</option>
          </select></label>
        <label class="field"><span>Heure (Casablanca)</span>
          <select v-model.number="forms.new.hour">
            <option v-for="hour in HOURS" :key="hour" :value="hour">{{ hour }} h</option>
          </select></label>
        <label class="field wide"><span>Rapport envoyé à</span>
          <input v-model="forms.new.notify" type="email" autocomplete="off" placeholder="manager@cabinet.ma"></label>
        <label v-if="forms.new.kind === 'instruction'" class="field wide"><span>Consigne donnée à Tarik à chaque
          fois</span>
          <textarea v-model="forms.new.prompt" rows="4" maxlength="2000"
            placeholder="Prépare la TVA de mes dossiers mensuels et dis-moi ce qui manque."></textarea></label>
        <label class="field wide"><span>Dossiers<template v-if="forms.new.kind === 'instruction'">
          (facultatif)</template></span>
          <select v-model="forms.new.dossier_ids" multiple size="6">
            <option v-for="dossier in data.dossiers" :key="dossier.id" :value="dossier.id">{{ dossier.name }}</option>
          </select></label>
      </fieldset>
      <div class="actions"><button type="submit" class="primary">Créer</button></div>
    </form>
  </template>
</template>

<style scoped>
.routine { display: grid; gap: .6rem; }
.routine-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: .6rem; }
.routine-head h2, .routine h2 { margin: 0; font-size: 1.05rem; }
.routine-fields { border: 0; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(12rem, 1fr)); gap: .7rem 1rem; }
.routine-fields .wide { grid-column: 1 / -1; }
.field { display: grid; gap: .25rem; }
.field span { font-size: .85rem; color: var(--muted); }
.field select, .field input, .field textarea { font: inherit; color: var(--fg); background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: .4rem .5rem; }
.check { display: flex; align-items: center; gap: .4rem; align-self: end; }
.actions { display: flex; gap: .6rem; }
.danger { color: var(--bad); }
.last pre { white-space: pre-wrap; max-height: 16rem; overflow: auto; font-size: .8rem; }
</style>
