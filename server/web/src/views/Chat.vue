<!-- The Discussion page: the team writes to a cabinet's Tarik, as the cabinet's clients would, with files. Without a
session in the address, the cabinet's conversations, latest first, each with its model calls' tokens and cost, read again at each change of the cabinet; with
one, that conversation, read again at each of its changes while Tarik works (each tool as it runs, then the reply),
and its form when it is one of the page's own. -->
<script setup>
import { computed, nextTick, ref, watch } from "vue";
import { useRoute } from "vue-router";

import { bulleted, cabinetDisplay, pickedCabinet, session, usePage } from "../api.js";
import { useLive } from "../live.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import ChatForm from "../components/ChatForm.vue";
import ChatTalk from "../components/ChatTalk.vue";
import Icon from "../components/Icon.vue";
import ModelUsage from "../components/ModelUsage.vue";
import PageState from "../components/PageState.vue";

const route = useRoute();
const talkId = route.params.session;
const { data, status, loading, reload } = usePage(() =>
  talkId ? `/discussion/${encodeURIComponent(talkId)}` : "/discussion");

const picked = pickedCabinet();
if (talkId) useLive(`session=${encodeURIComponent(talkId)}`, reload);
else if (picked) useLive(`cabinet=${encodeURIComponent(picked)}`, reload);

const shownName = computed(() => cabinetDisplay(data.value?.cabinet) || data.value?.cabinet);
const chips = { answering: "running", failed: "error", waiting: "question", answered: "ok" };
const stateName = (state) => session.labels.conversation_states?.[state] || state;
const talkLink = (item) => `/ui/discussion/${encodeURIComponent(item.session_id)}`;

// What a zip left out of the dashboard's first message, told here once the page is the conversation's.
const skipped = history.state?.skipped || [];

// Tarik done answering: the message box ready for the next one.
const form = ref(null);
watch(() => data.value?.going, async (going, was) => {
  if (!was || going) return;
  await nextTick();
  form.value?.focus();
});
</script>

<template>
  <PageState v-if="!data" :status="status" :loading="loading" />
  <template v-else-if="talkId">
    <nav class="crumbs" aria-label="Fil d'Ariane">
      <RouterLink to="/ui/discussion"><Icon name="chat" :size="14" />Discussions</RouterLink>
      <Icon name="next" :size="14" />
      <span aria-current="page">{{ shownName }}</span>
    </nav>
    <div class="head">
      <div>
        <h1>Discussion</h1>
        <p class="muted">Tarik se souvient de cette discussion, pas des autres.</p>
        <p class="muted">Modèle : <ModelUsage :usage="data.usage" full /></p>
      </div>
    </div>
    <section class="panel">
      <ChatTalk :session="data.session_id" :messages="data.messages" :latest="data.latest" :going="data.going" :made="data.made || []" />
      <p v-if="skipped.length" class="bad-text">Fichiers laissés de côté :{{ "\n" + bulleted(skipped) }}</p>
      <ChatForm
        v-if="data.written"
        ref="form"
        :session="data.session_id"
        :cabinet="data.cabinet"
        :checks="data.checks"
        :accepted="data.accepted"
        :limits="data.limits"
        :busy="data.going"
        @sent="reload"
      />
      <p v-else class="muted read-only">Discussion d'un fil d'emails ou d'un workflow : elle se lit ici, on n'y écrit pas. Pour écrire à Tarik, ouvrez une nouvelle discussion dans Discussions.</p>
    </section>
  </template>
  <template v-else>
    <div class="head">
      <div>
        <h1>Discussions<template v-if="data.cabinet"> · <CabinetLabel :name="data.cabinet" /></template></h1>
        <p class="muted">Toutes les discussions avec Tarik, la plus récente d'abord : celles écrites ici, comme un client du cabinet avec ses factures ou relevés joints, et les fils d'emails des clients, à lire seulement. Chaque message est une exécution, à suivre étape par étape.</p>
      </div>
      <RouterLink v-if="data.cabinet" class="primary" :to="`/ui/discussion/${data.new}`"><Icon name="plus" />Nouvelle discussion</RouterLink>
    </div>
    <div class="table-wrap">
      <table class="runs">
        <thead><tr><th>Dernier message</th><th>Canal</th><th>Premier message</th><th>État</th><th>Modèle</th></tr></thead>
        <tbody>
          <tr v-for="item in data.conversations" :key="item.session_id">
            <td class="num">{{ item.started }}</td>
            <td>
              <span v-if="item.routine" class="chip"><Icon name="clock" :size="13" /> Routine</span>
              <span v-else-if="item.medium === 'email'" class="chip"><Icon name="mail" :size="13" /> Email</span>
              <span v-else class="chip ok"><Icon name="chat" :size="13" /> Chat</span>
            </td>
            <td>
              <RouterLink :to="talkLink(item)"><template v-if="item.subject"><b>{{ item.subject }}</b> · </template>{{
                item.first || "(sans texte)" }}</RouterLink>
              <template v-if="item.sender"><br><span class="muted">{{ item.sender }}</span></template>
            </td>
            <td><span class="chip" :class="chips[item.state]">{{ stateName(item.state) }}</span></td>
            <td><ModelUsage :usage="item.usage" /></td>
          </tr>
          <tr v-if="!data.conversations.length"><td colspan="5" class="muted">Aucune discussion pour ce cabinet.</td></tr>
        </tbody>
      </table>
    </div>
  </template>
</template>
