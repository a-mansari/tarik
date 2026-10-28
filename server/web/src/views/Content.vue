<!-- One content document as a plain form, card by card: saved as a new version (admins, with why), its history (who,
when, why), what a version changed beside the text in service, and going back to it in a click; a card's fields
exported to a file, or imported from one. -->
<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { API, plural, send, session, usePage } from "../api.js";
import ContentImport from "../components/ContentImport.vue";
import ContentStatus from "../components/ContentStatus.vue";
import Icon from "../components/Icon.vue";
import PageState from "../components/PageState.vue";

const route = useRoute();
const router = useRouter();
const here = computed(() => `/tarik-prompts/${route.params.path}`);
const { data, status, loading, reload } = usePage((r) => {
  const shown = r.query.version ? `?version=${encodeURIComponent(r.query.version)}` : "";
  return `/tarik-prompts/${r.params.path}${shown}`;
});

// The fields as they are typed, which cards are open, which were typed in; set again from each reading.
const values = ref({});
const opens = ref([]);
const edited = ref([]);
const note = ref("");
const error = ref(null);
const saved = ref(null);
const busy = ref(false);
const told = ref(null);
const form = ref(null);

// How many of a section's fields the version shown changed (0 when no version is shown).
function touched(section) {
  if (!data.value.compare) return 0;
  return section.fields.filter((f) => f.key in data.value.compare.fields).length;
}
const changeOf = (f) => data.value.compare?.fields[f.key];
const count = (text) => `${text.length.toLocaleString("fr-FR")} caractères`;

// The version shown changed something, and it can be gone back to (an admin, not the version in service).
const compareChanged = computed(() => Object.keys(data.value.compare.fields).length > 0);
const canRestore = computed(() => {
  const { compare, versions } = data.value;
  return versions.length && compare.id !== versions[0].id && session.admin;
});

// A document with one card shows it open; a version shown opens the cards it changed; else the first.
function startsOpen(page, section, i) {
  if (page.sections.length === 1 || touched(section) > 0) return true;
  return !page.compare && i === 0;
}

watch(data, (page) => {
  if (!page?.sections) return;
  values.value = Object.fromEntries(page.sections.flatMap((s) => s.fields.map((f) => [f.key, f.value])));
  opens.value = page.sections.map((section, i) => startsOpen(page, section, i));
  edited.value = page.sections.map(() => false);
  nextTick(fitAll);
});

// Each text area as tall as its text; a closed card's areas have no height to measure: sized when it opens.
const BORDERS_PX = 2; // the area's top and bottom borders, beyond its scrollHeight
function fit(area) {
  area.style.height = "auto";
  area.style.height = `${area.scrollHeight + BORDERS_PX}px`;
}
function fitAll() {
  form.value?.querySelectorAll("textarea").forEach((area) => {
    if (area.offsetParent) fit(area);
  });
}
function toggled(i, event) {
  opens.value[i] = event.target.open;
  if (event.target.open) event.target.querySelectorAll("textarea").forEach(fit);
}
onMounted(() => window.addEventListener("resize", fitAll));
onBeforeUnmount(() => window.removeEventListener("resize", fitAll));

function cleared() {
  saved.value = null;
  error.value = null;
}
function allCards(open) {
  opens.value = opens.value.map(() => open);
  nextTick(fitAll);
}

const refusal = (answer) => answer.data?.error || `erreur ${answer.status}`;

async function save() {
  busy.value = true;
  const answer = await send(here.value, { fields: values.value, note: note.value });
  busy.value = false;
  if (!answer.ok) {
    error.value = refusal(answer);
    saved.value = null;
    allCards(true);
    return;
  }
  error.value = null;
  note.value = "";
  saved.value = answer.data.saved ? "1" : "0";
  await reload();
}

async function restore() {
  busy.value = true;
  const answer = await send(here.value, { action: "restore", version: data.value.compare.id });
  busy.value = false;
  if (!answer.ok) {
    error.value = refusal(answer);
    return;
  }
  await router.push(`/ui/tarik-prompts/${route.params.path}`);
  error.value = null;
  saved.value = answer.data.saved ? "1" : "0";
}
</script>

<template>
  <PageState v-if="!data?.sections" :status="status" :loading="loading" />
  <template v-else>
    <nav class="crumbs" aria-label="Fil d'Ariane">
      <RouterLink to="/ui/tarik-prompts"><Icon name="content" :size="14" />Tarik capabilities</RouterLink>
      <Icon name="next" :size="14" />
      <span aria-current="page">{{ data.title }}</span>
    </nav>
    <div class="head"><div>
      <h1>{{ data.title }}</h1>
      <p class="what">{{ data.what }}</p>
      <p v-if="data.model" class="muted">Modèle : <b>{{ data.model }}</b> (se change dans le code, pas ici).</p>
    </div></div>
    <div v-if="!session.admin" class="panel" role="status">Lecture seule : seul un administrateur modifie le contenu.</div>
    <div v-if="error" class="panel bad" role="alert"><b>Non enregistré :</b> {{ error }}. Corrigez puis enregistrez à nouveau.</div>
    <div v-if="saved === '1'" class="panel accent" role="status">Enregistré. Tarik l'utilise dès le message suivant.</div>
    <div v-else-if="saved === '0'" class="panel" role="status">Rien n'a changé : aucune nouvelle version.</div>
    <ContentStatus ref="told" @saved="reload" />
    <div v-if="data.compare" class="panel compare-banner">
      <p><b>Version {{ data.compare.id }}</b> :
        <template v-if="data.compare.first">la première, rien à comparer.</template>
        <template v-else-if="compareChanged">ce qu'elle a changé est montré dans chaque section concernée, à gauche du champ en service (→).</template>
        <template v-else>elle n'a rien changé.</template>
        Les champs gardent le texte en service.</p>
      <div class="panel-actions">
        <button v-if="canRestore" type="button" class="primary" :disabled="busy" @click="restore">Revenir à cette version</button>
        <RouterLink class="panel-button" :to="`/ui/tarik-prompts/${data.path}`" @click="cleared">Fermer</RouterLink>
      </div>
    </div>
    <div class="flow content-flow">
      <form id="content-form" ref="form" class="content-form" @submit.prevent="save">
        <fieldset class="plain" :disabled="!session.admin">
          <div v-if="data.sections.length > 1" class="card-tools">
            <button type="button" @click="allCards(true)">Tout déplier</button>
            <button type="button" @click="allCards(false)">Tout replier</button>
          </div>
          <!-- One card per section; closed cards' fields are still sent. -->
          <details
            v-for="(section, i) in data.sections"
            :key="i"
            :class="['panel', 'card', { touched: touched(section), changed: edited[i] }]"
            :open="opens[i]"
            @toggle="toggled(i, $event)"
            @input="edited[i] = true"
          >
            <summary>
              <span class="card-title">{{ section.title || data.title }}</span>
              <span class="card-meta">
                <span v-if="touched(section)" class="chip wait">
                  {{ touched(section) }} changé{{ plural(touched(section)) }} en version {{ data.compare.id }}
                </span>
                <span class="muted">{{ section.fields.length }} champ{{ plural(section.fields.length) }}</span>
                <a
                  class="card-action"
                  :href="`${API}${here}?export=${i}`"
                  download
                  :data-tip="data.tips.section_export"
                  :aria-description="data.tips.section_export"
                  @click.stop
                >Exporter</a>
                <ContentImport
                  v-if="session.admin"
                  class="card-action"
                  :to="here"
                  :tip="data.tips.section_import"
                  @done="(answer) => told?.tell(answer)"
                >Importer</ContentImport>
              </span>
            </summary>
            <div class="card-body">
              <template v-for="f in section.fields" :key="f.key">
                <div v-if="changeOf(f)" class="field-pair">
                  <!-- The version's change (old) beside the text in service (new), an arrow from one to the other. -->
                  <div class="field">
                    <span>{{ f.label }} : changé en version {{ data.compare.id }}</span>
                    <div class="field-diff">
                      <p class="diff">
                        <template v-for="([kind, part], j) in changeOf(f).parts" :key="j">
                          <ins v-if="kind === '+'">{{ part }}</ins>
                          <del v-else-if="kind === '-'">{{ part }}</del>
                          <template v-else>{{ part }}</template>
                        </template>
                      </p>
                    </div>
                    <small class="count num">{{ changeOf(f).before }} → {{ changeOf(f).after }} caractères</small>
                  </div>
                  <span class="arrow" aria-hidden="true">→</span>
                  <label class="field">
                    <span>{{ f.label }} : en service</span>
                    <template v-if="f.long">
                      <textarea v-model="values[f.key]" :name="f.key" rows="3" @input="fit($event.target)"></textarea>
                      <small class="count num" aria-live="off">{{ count(values[f.key]) }}</small>
                    </template>
                    <template v-else>
                      <input v-model="values[f.key]" :name="f.key">
                      <small class="count num">{{ count(values[f.key]) }}</small>
                    </template>
                  </label>
                </div>
                <label v-else class="field">
                  <span>{{ f.label }}</span>
                  <template v-if="f.long">
                    <textarea v-model="values[f.key]" :name="f.key" rows="3" @input="fit($event.target)"></textarea>
                    <small class="count num" aria-live="off">{{ count(values[f.key]) }}</small>
                  </template>
                  <input v-else v-model="values[f.key]" :name="f.key">
                </label>
              </template>
            </div>
          </details>
        </fieldset>
      </form>
      <div class="side-column">
        <section v-if="session.admin" class="panel save-card" aria-label="Enregistrer">
          <h2>Enregistrer</h2>
          <label class="field">
            <span>Pourquoi ce changement (gardé dans l'historique)</span>
            <textarea
              v-model="note"
              maxlength="300"
              rows="2"
              placeholder="ex. : demander l'exercice avant le compte"
            ></textarea>
          </label>
          <button type="submit" form="content-form" class="primary" :disabled="busy">Enregistrer</button>
        </section>
        <aside class="side panel">
          <h2>Historique</h2>
          <p class="muted">Cliquez sur une version pour voir, dans chaque section, ce qu'elle a changé.</p>
          <ol class="versions">
            <li v-for="(v, i) in data.versions" :key="v.id" :aria-current="data.compare?.id === v.id ? 'true' : null">
              <RouterLink :to="`/ui/tarik-prompts/${data.path}?version=${v.id}`" @click="cleared">
                <span class="num">{{ v.at }}</span> <b>{{ v.author }}</b> <span v-if="i === 0" class="chip ok">en service</span>
                <span v-if="v.note" class="muted">{{ v.note }}</span>
              </RouterLink>
            </li>
          </ol>
        </aside>
      </div>
    </div>
  </template>
</template>
