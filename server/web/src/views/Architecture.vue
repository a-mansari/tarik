<!-- Tarik's architecture: the map of his harness (HarnessMap.vue), a general page like Workflows. The tools, skills and
channels are the same for every cabinet; the model, the mailbox, the routines and the test mode are the cabinet picked in
the sidebar's, named here. -->
<script setup>
import { computed } from "vue";

import { session } from "../api.js";
import CabinetLabel from "../components/CabinetLabel.vue";
import HarnessMap from "../components/HarnessMap.vue";

const cabinet = computed(() => session.cabinets.find((found) => found.chosen)?.name);
</script>

<template>
  <div class="head">
    <div>
      <h1>Architecture de Tarik</h1>
      <p v-if="cabinet" class="muted">
        Les mêmes outils et compétences pour tous les cabinets. Le modèle, la boîte email, les routines et le mode test
        sont ceux de <CabinetLabel :name="cabinet" />.
      </p>
    </div>
  </div>
  <HarnessMap v-if="cabinet" />
  <div v-else class="panel">Aucun cabinet.</div>
</template>
