<script setup>
import { ref } from "vue";
import { useRoute } from "vue-router";

import { send } from "../api.js";

const route = useRoute();
const email = ref("");
const password = ref("");
const failure = ref("");

// What went wrong, by the server's status; a closed or unset account stays a wrong login (never said apart).
const FAILURES = {
  401: "Email ou mot de passe incorrect. Après 5 essais, attendez une minute.",
  403: "Connexion refusée : cette page n'est pas ouverte depuis l'adresse de Tarik. Prévenez l'administrateur.",
  429: "Trop d'essais : attendez une minute avant de réessayer.",
};

async function login() {
  failure.value = "";
  const next = route.query.next || "";
  const body = { email: email.value, password: password.value };
  let answer;
  try {
    answer = await send(`/login?next=${encodeURIComponent(next)}`, body, "POST", { keepOnLogin: true });
  } catch {
    failure.value = "Tarik est injoignable : vérifiez votre connexion, puis réessayez.";
    return;
  }
  if (!answer.ok) {
    failure.value = FAILURES[answer.status] || `Tarik ne répond pas comme prévu (erreur ${answer.status}) : réessayez dans un instant.`;
    return;
  }
  location.assign(answer.data?.next || "/"); // the whole app again, with the session
}
</script>

<template>
  <section class="panel login">
    <h1>Connexion</h1>
    <p class="muted">Les pages de Tarik montrent les documents et les mots des clients : réservées à l'équipe.</p>
    <p v-if="route.query.ready" role="status">Mot de passe enregistré : connectez-vous.</p>
    <p v-if="failure" class="bad-text" role="alert">{{ failure }}</p>
    <form @submit.prevent="login">
      <label>Email <input v-model="email" type="email" name="email" autocomplete="username" required autofocus></label>
      <label>Mot de passe <input v-model="password" type="password" name="password" autocomplete="current-password" required></label>
      <button type="submit">Se connecter</button>
    </form>
  </section>
</template>
