<!-- Tarik's capabilities: everything the team may change without touching code, by kind (his instructions, his tools,
his skills, how documents are read, the pages' words), each page a card to open, with who changed it last; the whole
content (or a group) exported, or an export imported (admins). -->
<script setup>
import { ref } from "vue";
import { API, plural, session, usePage } from "../api.js";
import ContentImport from "../components/ContentImport.vue";
import ContentStatus from "../components/ContentStatus.vue";
import Icon from "../components/Icon.vue";
import PageState from "../components/PageState.vue";

const { data, status, loading, reload } = usePage(() => "/tarik-prompts");
const told = ref(null);
const groupTip = (tip, name) => tip.replace("{group}", name);
// Each kind at a glance: its icon, and what one line of it is.
const KINDS = {
  tarik: { icon: "chat" },
  outils: { icon: "settings", counted: "outil" },
  competences: { icon: "content" },
  consignes: { icon: "file" },
  pages: { icon: "workflows" },
};
</script>

<template>
  <PageState v-if="!data" :status="status" :loading="loading" />
  <template v-else>
    <div class="head">
      <div>
        <h1>Tarik capabilities</h1>
        <p class="muted">Modifiable sans code · chaque changement gardé et réversible · appliqué au message suivant.</p>
      </div>
      <div class="panel-actions">
        <a class="panel-button" :href="`${API}/tarik-prompts-export`" download :data-tip="data.tips.all_export"
          :aria-description="data.tips.all_export"><Icon name="download" :size="14" /> Exporter tout</a>
        <ContentImport v-if="session.admin" class="panel-button" to="/tarik-prompts-import" :tip="data.tips.all_import"
          @done="(answer) => told?.tell(answer)"><Icon name="attach" :size="14" /> Importer</ContentImport>
      </div>
    </div>
    <ContentStatus ref="told" @saved="reload" />
    <div v-if="data.empty" class="panel bad" role="alert"><b>Aucun contenu.</b> Tarik ne peut pas répondre sans ses
      instructions : importez un export (« Importer »).</div>
    <section v-for="group in data.groups" :key="group.key" class="capability-group" :aria-label="group.name">
      <div class="capability-head">
        <span class="capability-icon"><Icon :name="KINDS[group.key]?.icon || 'content'" :size="16" /></span>
        <h2>{{ group.name }}</h2>
        <span class="chip">{{ group.documents.length }}</span>
        <span class="capability-tools">
          <a class="icon-button" :href="`${API}/tarik-prompts-export?group=${group.key}`" download
            :data-tip="groupTip(data.tips.group_export, group.name)"
            :aria-label="groupTip(data.tips.group_export, group.name)"><Icon name="download" :size="14" /></a>
          <ContentImport v-if="session.admin" class="icon-button" to="/tarik-prompts-import"
            :tip="groupTip(data.tips.group_import, group.name)" @done="(answer) => told?.tell(answer)"
            :aria-label="groupTip(data.tips.group_import, group.name)"><Icon name="attach" :size="14" /></ContentImport>
        </span>
      </div>
      <ul class="capability-cards">
        <li v-for="document in group.documents" :key="document.path">
          <RouterLink class="capability-card" :to="`/ui/tarik-prompts/${document.path}`">
            <b>{{ document.title }}</b>
            <span v-if="KINDS[group.key]?.counted" class="chip">{{ document.fields }} {{ KINDS[group.key].counted }}{{ plural(document.fields) }}</span>
            <span v-if="document.change" class="muted capability-change">
              <Icon name="clock" :size="12" /> {{ document.change.author }} · {{ document.change.at }}</span>
          </RouterLink>
        </li>
        <li v-if="!group.documents.length" class="muted">Aucun document.</li>
      </ul>
    </section>
  </template>
</template>

<style scoped>
.capability-group { margin-top: 1.4rem; }
.capability-head { display: flex; align-items: center; gap: .5rem; margin-bottom: .6rem; }
.capability-head h2 { margin: 0; font-size: 1.05rem; }
.capability-icon { display: inline-grid; place-items: center; width: 1.9rem; height: 1.9rem; border-radius: 8px; background: var(--accent-soft); color: var(--accent); }
.capability-tools { margin-left: auto; display: flex; gap: .3rem; }
.icon-button { display: inline-grid; place-items: center; width: 1.9rem; height: 1.9rem; border: 1px solid var(--line); border-radius: 6px; background: var(--surface); color: var(--muted); cursor: pointer; font: inherit; }
.icon-button:hover { color: var(--accent); border-color: var(--accent); }
.icon-button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.capability-cards { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(15rem, 1fr)); gap: .6rem; }
.capability-card { display: grid; gap: .35rem; align-content: start; height: 100%; box-sizing: border-box; padding: .75rem .85rem; border: 1px solid var(--line); border-left: 3px solid var(--accent); border-radius: 8px; background: var(--surface); color: var(--fg); text-decoration: none; }
.capability-card:hover { border-color: var(--accent); background: var(--accent-soft); }
.capability-card:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.capability-card .chip { justify-self: start; }
.capability-change { display: flex; align-items: center; gap: .3rem; font-size: .8rem; }
</style>
