<!-- The Routines page: the routines of the cabinet picked in the sidebar, only that cabinet's, as cards: turned on or
off with a switch, changed through the same form a new one is made with, deleted. The server checks each change as
Tarik's own routine tool does (server/api/routines.py) and keeps it in the Logs serveur. A refused change keeps what
was typed. -->
<script setup>
import { nextTick, ref } from "vue";

import { send, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import Icon from "../components/Icon.vue";
import PageState from "../components/PageState.vue";
import RoutineForm from "../components/RoutineForm.vue";

const { data, status, loading } = usePage(() => "/routines");
const editing = ref(null); // "new", a routine's id, or null
const form = ref(null);
const busy = ref(false);
const banner = ref(null);
const shown = ref(0); // bumped after a refused switch: the cards are drawn again, as the server holds them

const blank = () => ({
  enabled: true, kind: "instruction", every: "week", days: [0, 1, 2, 3, 4], hour: 8, notify: "", prompt: "",
});

// The form opens where it is asked, its chosen task focused (the button that opened it may be gone); the focus ring
// shows only for the keyboard, so a click leaves the chosen card alone highlighted.
async function open(routine) {
  editing.value = routine ? routine.id : "new";
  form.value = routine ? { ...routine, days: [...routine.days] } : blank();
  await nextTick();
  document.querySelector(".routine-form input[name=kind]:checked")?.focus();
}
const close = () => { editing.value = null; form.value = null; };

async function post(action, routine) {
  busy.value = true;
  const answer = await send("/routines", { cabinet: data.value.cabinet, action, routine });
  busy.value = false;
  if (answer.data) data.value = { ...data.value, ...answer.data }; // a change's answer leaves Experio's admins out
  if (answer.ok && routine === form.value) close(); // saved: the form closes; refused: it stays as typed
  await nextTick();
  if (!answer.ok) banner.value?.scrollIntoView({ block: "center" });
  return answer.ok;
}

const submit = () => post(editing.value === "new" ? "create" : "save", form.value);
async function toggle(routine) {
  const saved = await post("save", { ...routine, enabled: !routine.enabled });
  if (!saved) shown.value += 1; // the switch back as it was
  else if (form.value?.id === routine.id) form.value.enabled = !routine.enabled; // an open form keeps up
}
function remove(routine) {
  if (confirm(`Supprimer la routine n° ${routine.id} ? Ses rapports passés sont oubliés.`)) post("delete", routine);
}
</script>

<template>
  <PageState v-if="!data || status === 403 || status === 404" :status="status" :loading="loading" />
  <template v-else>
    <div class="head">
      <div>
        <h1>Routines · <CabinetLabel :name="data.cabinet" /></h1>
        <p class="what">Ce que Tarik refait à date fixe pour ce cabinet, sur tous les dossiers que voit son compte
          Experio, et dont il envoie le rapport par e-mail à un administrateur du cabinet. Une routine prépare et
          rapporte : elle ne déclare, ne comptabilise et ne confirme jamais rien.</p>
      </div>
      <button v-if="editing !== 'new'" type="button" class="primary" @click="open(null)">
        <Icon name="plus" /> Nouvelle routine</button>
    </div>
    <div ref="banner">
      <div v-if="data.error" class="panel bad" role="alert"><b>Non enregistré :</b> {{ data.error }}.</div>
    </div>

    <section v-if="editing === 'new'" class="panel accent" aria-label="Nouvelle routine">
      <h2>Nouvelle routine</h2>
      <RoutineForm :form="form" :kinds="data.kinds" :weekdays="data.weekdays" :managers="data.managers" :busy="busy"
        submit-words="Créer la routine" @submit="submit" @cancel="close" />
    </section>

    <article v-for="routine in data.routines" :key="`${routine.id}-${shown}`" class="panel routine">
      <div class="routine-head">
        <label class="switch" :title="routine.enabled ? 'Mettre en pause' : 'Activer'">
          <input type="checkbox" :checked="routine.enabled" :disabled="busy" @change="toggle(routine)">
          <span class="chip" :class="routine.enabled ? 'ok' : ''">{{ routine.enabled ? "Active" : "En pause" }}</span>
        </label>
        <div class="routine-what">
          <b>{{ data.kinds[routine.kind] }}</b>
          <span v-if="routine.kind === 'instruction'" class="prompt">« {{ routine.prompt }} »</span>
          <span class="muted">{{ routine.schedule }} · rapport à {{ routine.notify }}</span>
        </div>
        <div class="routine-tools">
          <button type="button" class="panel-button" @click="editing === routine.id ? close() : open(routine)">
            {{ editing === routine.id ? "Fermer" : "Modifier" }}</button>
          <button type="button" class="panel-button" @click="remove(routine)">Supprimer</button>
        </div>
      </div>
      <p class="muted when">n° {{ routine.id }} · prochaine le <span class="num">{{ routine.next_run_at }}</span> · créée
        par {{ routine.created_by }}<template v-if="routine.last_due"> · dernière le
          <span class="num">{{ routine.last_due }}</span>{{ routine.last_report === null ? " (en cours)" : "" }}</template></p>
      <details v-if="routine.last_report" class="last"><summary>Dernier rapport</summary><pre>{{ routine.last_report }}</pre></details>
      <RoutineForm v-if="editing === routine.id" :form="form" :kinds="data.kinds" :weekdays="data.weekdays"
        :managers="data.managers" :busy="busy" @submit="submit" @cancel="close" />
    </article>
    <div v-if="!data.routines.length && editing !== 'new'" class="panel empty">
      <p><b>Aucune routine pour ce cabinet.</b></p>
      <p class="muted">Une routine fait faire à Tarik une tâche à date fixe : préparer la TVA chaque mois, envoyer
        l'avancement chaque lundi, ou suivre une consigne que vous écrivez.</p>
      <button type="button" class="primary" @click="open(null)"><Icon name="plus" /> Créer la première</button>
    </div>
  </template>
</template>

<style scoped>
.routine { display: grid; gap: .5rem; }
.routine-head { display: flex; flex-wrap: wrap; align-items: center; gap: .8rem 1rem; }
.routine-what { display: grid; gap: .15rem; flex: 1 1 18rem; min-width: 0; }
.prompt { overflow-wrap: anywhere; }
.routine-tools { display: flex; gap: .4rem; }
.when { margin: 0; font-size: .85rem; }
.last pre { white-space: pre-wrap; max-height: 16rem; overflow: auto; font-size: .8rem; }
.empty { display: grid; gap: .4rem; justify-items: start; }
.empty p { margin: 0; }
section h2 { margin: 0 0 .8rem; font-size: 1.05rem; }
</style>
