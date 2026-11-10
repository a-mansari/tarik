<!-- The WhatsApp page, an admin's: which of the cabinet's collaborators write to Tarik on WhatsApp. A collaborator is
enrolled with the phone number of their Experio user (Tarik's own host is registered at Sparrow for it, and they
receive a welcome message), and can be removed again. -->
<script setup>
import { ref } from "vue";

import { send, usePage } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import PageState from "../components/PageState.vue";

const { data, status, loading } = usePage(() => "/whatsapp");

const STATES = {
  enrolled: { chip: "ok", words: "Activé" },
  ready: { chip: "", words: "Prêt à activer" },
  no_phone: { chip: "wait", words: "Pas de numéro dans Experio" },
  foreign: { chip: "wait", words: "Numéro non marocain" },
};

// One action at a time: the buttons wait for the answer (an enrollment can take several seconds).
const busy = ref(false);

async function act(action, person) {
  if (action === "remove" && !confirm(`Retirer ${person.login} de WhatsApp ? Le numéro n'écrira plus à Tarik.`)) return;
  busy.value = true;
  try {
    // The phone is the one shown: the server refuses if Experio has another one by now.
    const answer = await send("/whatsapp", { action, login: person.login, phone: person.phone, cabinet: data.value.cabinet });
    if (answer.data?.cabinet) data.value = answer.data; // an answer without the page's data (a 404) leaves the page as it is
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <PageState v-if="!data || status === 403 || status === 404" :status="status" :loading="loading" />
  <template v-else-if="data.unreachable">
    <h1><CabinetLabel :name="data.cabinet" /> · WhatsApp</h1>
    <div v-if="data.error" class="panel bad" role="alert"><b>Non fait :</b> {{ data.error }}</div>
    <div v-if="data.done" class="panel accent" role="status">{{ data.done }}</div>
    <div class="panel bad" role="alert">Experio ne répond pas pour ce cabinet : la liste des collaborateurs est introuvable pour le moment. Réessayez dans un instant.</div>
  </template>
  <template v-else>
    <div class="head"><div>
      <h1><CabinetLabel :name="data.cabinet" /> · WhatsApp</h1>
    </div></div>
    <div v-if="data.error" class="panel bad" role="alert"><b>Non fait :</b> {{ data.error }}</div>
    <div v-if="data.done" class="panel accent" role="status">{{ data.done }}</div>
    <div class="table-wrap">
      <table class="runs accounts">
        <thead><tr><th>Collaborateur</th><th>Numéro</th><th>État</th><th>Action</th></tr></thead>
        <tbody>
          <tr v-for="p in data.people" :key="p.login">
            <td><b>{{ p.name }}</b><br><span class="mono muted">{{ p.login }}</span></td>
            <td class="mono">{{ p.phone || "—" }}</td>
            <td><span class="chip" :class="STATES[p.state].chip">{{ STATES[p.state].words }}</span></td>
            <td class="actions">
              <button v-if="p.state === 'ready'" type="button" class="panel-button" :disabled="busy" @click="act('enroll', p)">{{ busy ? "Patientez…" : "Activer" }}</button>
              <button v-else-if="p.state === 'enrolled'" type="button" class="panel-button" :disabled="busy" @click="act('remove', p)">Retirer</button>
            </td>
          </tr>
          <tr v-if="!data.people.length"><td colspan="4" class="muted">Ce cabinet n'a aucun collaborateur dans Experio.</td></tr>
        </tbody>
      </table>
    </div>
  </template>
</template>
