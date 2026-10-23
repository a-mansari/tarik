<!-- The cabinet picked: its last days in four numbers, read again at each change of the cabinet as runs happen, and a
message to Tarik that opens a new conversation: once sent, the page becomes that conversation's (its address too, the
back button back here). -->
<script setup>
import { useRouter } from "vue-router";

import { pickedCabinet, usePage } from "../api.js";
import { useLive } from "../live.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import ChatForm from "../components/ChatForm.vue";
import PageState from "../components/PageState.vue";

const router = useRouter();
const { data, status, loading, reload } = usePage(() => "/accueil");
const picked = pickedCabinet();
if (picked) useLive(`cabinet=${encodeURIComponent(picked)}`, reload);

const opened = (answer) =>
  router.push({ path: `/ui/discussion/${data.value.new}`, state: { skipped: answer.skipped || [] } });
</script>

<template>
  <PageState v-if="!data" :status="status" :loading="loading" />
  <div v-else-if="!data.cabinet" class="panel">Aucun cabinet.</div>
  <div v-else class="dashboard">
    <div class="head">
      <div>
        <h1 id="dashboard-title"><CabinetLabel :name="data.cabinet" /></h1>
        <p id="dashboard-what" class="muted">Les {{ data.days }} derniers jours de Tarik pour ce cabinet.</p>
      </div>
    </div>
    <section class="metrics" :aria-label="`Les ${data.days} derniers jours`">
      <div class="metric"><span class="value num">{{ data.metrics.runs }}</span><span class="what">exécutions</span></div>
      <div class="metric"><span class="value num">{{ data.metrics.seconds }}</span><span class="what">durée moyenne</span></div>
      <div class="metric" :class="{ bad: data.metrics.bad }"><span class="value num">{{ data.metrics.failed }}</span><span class="what">en échec</span></div>
      <div class="metric"><span class="value num">{{ data.metrics.conversations }}</span><span class="what">discussions</span></div>
    </section>
    <section class="panel chat-start">
      <h2 class="ask">Écrire à Tarik</h2>
      <ChatForm
        :session="data.new"
        :cabinet="data.cabinet"
        :checks="data.checks"
        :accepted="data.accepted"
        :limits="data.limits"
        @sent="opened"
      />
    </section>
  </div>
</template>
