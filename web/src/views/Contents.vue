<!-- The content's list: every document the team may change without touching code, by kind, with who changed it last;
the whole content (or a group) exported, or an export imported (admins). -->
<script setup>
import { ref } from "vue";
import { API, session, usePage } from "../api.js";
import ContentImport from "../components/ContentImport.vue";
import ContentStatus from "../components/ContentStatus.vue";
import PageState from "../components/PageState.vue";

const { data, status, loading, reload } = usePage(() => "/tarik-prompts");
const told = ref(null);
const groupTip = (tip, name) => tip.replace("{group}", name);
</script>

<template>
  <PageState v-if="!data" :status="status" :loading="loading" />
  <template v-else>
    <div class="head">
      <div>
        <h1>Tarik prompts</h1>
        <p class="muted">Ce que Tarik dit et ce qu'on lui dit, le même pour tous les cabinets pour l'instant, modifiable sans toucher au code. Chaque modification est gardée (qui, quand, pourquoi) et peut être annulée en un clic. Elle s'applique au message suivant. Le contenu vit dans la base de données : exportez-le pour le garder ou le copier vers une autre installation.</p>
      </div>
      <div class="panel-actions">
        <a
          class="panel-button"
          :href="`${API}/tarik-prompts-export`"
          download
          :data-tip="data.tips.all_export"
          :aria-description="data.tips.all_export"
        >Exporter tout</a>
        <ContentImport
          v-if="session.admin"
          class="panel-button"
          to="/tarik-prompts-import"
          :tip="data.tips.all_import"
          @done="(answer) => told?.tell(answer)"
        >Importer un export</ContentImport>
      </div>
    </div>
    <ContentStatus ref="told" @saved="reload" />
    <div v-if="data.empty" class="panel bad" role="alert"><b>Aucun contenu.</b> Tarik ne peut pas répondre sans ses instructions : importez un export du contenu (bouton « Importer un export »).</div>
    <section v-for="group in data.groups" :key="group.key" class="panel">
      <div class="group-head">
        <h2>{{ group.name }}</h2>
        <span class="panel-actions">
          <a
            class="card-action"
            :href="`${API}/tarik-prompts-export?group=${group.key}`"
            download
            :data-tip="groupTip(data.tips.group_export, group.name)"
            :aria-description="groupTip(data.tips.group_export, group.name)"
          >Exporter</a>
          <ContentImport
            v-if="session.admin"
            class="card-action"
            to="/tarik-prompts-import"
            :tip="groupTip(data.tips.group_import, group.name)"
            @done="(answer) => told?.tell(answer)"
          >Importer</ContentImport>
        </span>
      </div>
      <ul class="runs-side">
        <li v-for="document in group.documents" :key="document.path">
          <RouterLink :to="`/ui/tarik-prompts/${document.path}`">
            <span>{{ document.title }}</span>
            <span class="muted"><template v-if="document.change">{{ document.change.author }} · {{ document.change.at }}</template></span>
          </RouterLink>
        </li>
        <li v-if="!group.documents.length" class="muted">Aucun document.</li>
      </ul>
    </section>
  </template>
</template>
