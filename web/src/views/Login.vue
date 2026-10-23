<script setup>
import { ref } from "vue";
import { useRoute } from "vue-router";

import { send } from "../api.js";

const route = useRoute();
const email = ref(""), password = ref(""), failed = ref(false);

async function login() {
  const next = route.query.next || "";
  const answer = await send(`/login?next=${encodeURIComponent(next)}`, { email: email.value, password: password.value },
    "POST", { keepOnLogin: true });
  if (!answer.ok) { failed.value = true; return; }
  location.assign(answer.data.next); // the whole app again, with the session
}
</script>

<template>
  <section class="panel login">
    <h1>Connexion</h1>
    <p class="muted">Les pages de Tarik montrent les documents et les mots des clients : réservées à l'équipe.</p>
    <p v-if="route.query.ready" role="status">Mot de passe enregistré : connectez-vous.</p>
    <p v-if="failed" class="bad-text" role="alert">Email ou mot de passe incorrect. Après 5 essais, attendez une minute.</p>
    <form @submit.prevent="login">
      <label>Email <input v-model="email" type="email" name="email" autocomplete="username" required autofocus></label>
      <label>Mot de passe <input v-model="password" type="password" name="password" autocomplete="current-password" required></label>
      <button type="submit">Se connecter</button>
    </form>
  </section>
</template>
