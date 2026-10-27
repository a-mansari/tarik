<!-- The Routines page: the routines of the cabinet picked in the sidebar, only that cabinet's. Each one turned on or
off, rescheduled, its instruction, address and dossiers changed, or deleted; a new one made. The server checks each
change as Tarik's own routine tool does (server/api/routines.py), and keeps it in the Logs serveur. A refused change
keeps what was typed; a saved one is shown as the server holds it. -->
<script setup>
import { computed, nextTick, ref, watch } from "vue";

import { send, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import PageState from "../components/PageState.vue";
import RoutineForm from "../components/RoutineForm.vue";

const { data, status, loading } = usePage(() => "/routines");
const forms = ref({}); // routine id -> its form; "new" -> the new routine's
const banner = ref(null);

const blank = () => ({
  enabled: true, kind: "instruction", dossier_ids: [], every: "month", day: 5, hour: 8, notify: "", prompt: "",
});
const formOf = (routine) => ({ ...routine, dossier_ids: [...routine.dossier_ids] });

// The page as first read: every form as the server holds it.
watch(data, (shown, before) => {
  if (before || !shown?.routines) return;
  forms.value = { new: blank(), ...Object.fromEntries(shown.routines.map((r) => [r.id, formOf(r)])) };
}, { immediate: true });

const names = computed(() => new Map((data.value?.dossiers || []).map((dossier) => [dossier.id, dossier.name])));

async function act(action, key) {
  if (action === "delete" && !confirm("Supprimer cette routine ? Ses rapports passés sont oubliés.")) return;
  const answer = await send("/routines", { cabinet: data.value.cabinet, action, routine: forms.value[key] });
  if (!answer.data) return;
  data.value = { ...data.value, ...answer.data }; // a change's answer leaves the dossiers out: kept from the page
  if (answer.ok) {
    // Saved: its form as the server holds it now, every other form as typed; a new routine's form starts again.
    const held = Object.fromEntries(answer.data.routines.map((r) => [r.id, formOf(r)]));
    const kept = Object.fromEntries(Object.entries(forms.value).filter(([id]) => id !== String(key) && id in held));
    forms.value = { ...held, ...kept, new: action === "create" ? blank() : forms.value.new };
  }
  await nextTick();
  banner.value?.scrollIntoView({ block: "center" });
}
</script>

<template>
  <PageState v-if="!data || status === 403 || status === 404" :status="status" :loading="loading" />
  <div v-else-if="!data.routines" class="panel bad" role="alert"><b>Page indisponible :</b> {{ data.error }}</div>
  <template v-else>
    <div class="head"><div>
      <h1>Routines · <CabinetLabel :name="data.cabinet" /></h1>
      <p class="what">Ce que Tarik refait à date fixe pour ce cabinet, et dont il envoie le rapport par e-mail. Une
        routine prépare et rapporte, sur ses seuls dossiers : elle ne déclare, ne comptabilise et ne confirme jamais
        rien. L'adresse du rapport est revérifiée à chaque fois : elle doit encore avoir ces dossiers dans Experio.</p>
    </div></div>
    <div ref="banner">
      <div v-if="data.error" class="panel bad" role="alert"><b>Non enregistré :</b> {{ data.error }}.</div>
    </div>

    <form v-for="routine in data.routines" :key="routine.id" class="panel routine" @submit.prevent="act('save', routine.id)">
      <div class="routine-head">
        <h2>n° {{ routine.id }} · {{ data.kinds[routine.kind] }}</h2>
        <span class="chip" :class="routine.enabled ? 'ok' : ''">{{ routine.enabled ? "Active" : "En pause" }}</span>
      </div>
      <p class="muted">{{ routine.schedule }}, prochaine le <span class="num">{{ routine.next_run_at }}</span> · créée par
        {{ routine.created_by }}<template v-if="routine.last_due"> · dernière le
          <span class="num">{{ routine.last_due }}</span>{{ routine.last_report === null ? " (en cours)" : "" }}</template></p>
      <details v-if="routine.last_report" class="last"><summary>Dernier rapport</summary><pre>{{ routine.last_report }}</pre></details>
      <RoutineForm v-if="forms[routine.id]" :form="forms[routine.id]" :kinds="data.kinds" :weekdays="data.weekdays"
        :dossiers="data.dossiers || []" :names="names" />
      <div class="panel-actions">
        <button type="submit" class="primary">Enregistrer</button>
        <button type="button" class="panel-button" @click="act('delete', routine.id)">Supprimer</button>
      </div>
    </form>
    <p v-if="!data.routines.length" class="muted">Aucune routine pour ce cabinet.</p>

    <form v-if="forms.new" class="panel routine accent" @submit.prevent="act('create', 'new')">
      <h2>Nouvelle routine</h2>
      <RoutineForm :form="forms.new" :kinds="data.kinds" :weekdays="data.weekdays" :dossiers="data.dossiers || []"
        :names="names" />
      <div class="panel-actions"><button type="submit" class="primary">Créer</button></div>
    </form>
  </template>
</template>

<style scoped>
.routine { display: grid; gap: .6rem; }
.routine-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: .6rem; }
.routine h2 { margin: 0; font-size: 1.05rem; }
.last pre { white-space: pre-wrap; max-height: 16rem; overflow: auto; font-size: .8rem; }
</style>
