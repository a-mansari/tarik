<!-- An invitation's page, open without a session: the person a one-time link is for chooses their password (twice),
then goes to the login. -->
<script setup>
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { send, usePage } from "../api.js";

const route = useRoute();
const router = useRouter();
const { data, status, loading } = usePage((r) => `/invitation/${encodeURIComponent(r.params.token)}`);
const password = ref("");
const again = ref("");
const capitalized = (text) => text.charAt(0).toUpperCase() + text.slice(1);

async function choose() {
  const body = { password: password.value, again: again.value };
  const answer = await send(`/invitation/${encodeURIComponent(route.params.token)}`, body, "POST", { keepOnLogin: true });
  if (answer.ok && answer.data?.next) return router.push(answer.data.next);
  if (answer.data) data.value = answer.data;
  status.value = answer.status;
}
</script>

<template>
  <p v-if="loading" class="muted" role="status">Chargement…</p>
  <section v-else class="panel login">
    <template v-if="status === 404 || data?.gone">
      <h1>Lien plus valable</h1>
      <p>Ce lien a déjà servi ou a expiré. Demandez-en un nouveau à un administrateur.</p>
    </template>
    <template v-else-if="data?.found">
      <h1>Choisir votre mot de passe</h1>
      <p class="muted">Pour <b>{{ data.found.name }}</b> ({{ data.found.email }}). {{ data.min }} caractères au moins. Ce lien ne sert qu'une fois.</p>
      <p v-if="data.error" class="bad-text" role="alert">{{ capitalized(data.error) }}.</p>
      <form @submit.prevent="choose">
        <label class="field">
          <span>Mot de passe</span>
          <input v-model="password" type="password" name="password" :minlength="data.min" required autocomplete="new-password">
        </label>
        <label class="field">
          <span>Encore</span>
          <input v-model="again" type="password" name="again" :minlength="data.min" required autocomplete="new-password">
        </label>
        <button type="submit" class="primary">Enregistrer</button>
      </form>
    </template>
    <p v-else class="bad-text" role="alert">{{ data?.error || "Page indisponible." }}</p>
  </section>
</template>
