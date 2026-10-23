<!-- The Comptes page, an admin's: every account, one made for a colleague (with a one-time link to send them, for
them to choose their password), a role changed, an account closed, a new link (a forgotten password). A new link is
shown once, from the answer to the action: reading the page again never shows it. -->
<script setup>
import { ref } from "vue";

import { roleName, send, usePage } from "../api.js";
import PageState from "../components/PageState.vue";

const { data, status, loading } = usePage(() => "/comptes");
const blank = () => ({ email: "", name: "", role: "member" });
const added = ref(blank());
const copied = ref(false);
const linkField = ref(null);

async function act(body, question) {
  if (question && !confirm(question)) return;
  const answer = await send("/comptes", body);
  if (!answer.data) return;
  data.value = answer.data;
  copied.value = false;
  if (body.action === "add" && answer.ok) added.value = blank();
}

const otherRole = (a) => (a.role === "admin" ? "member" : "admin");
const roleButton = (a) => (a.role === "admin" ? "Rendre membre" : "Rendre administrateur");

async function copy() {
  linkField.value.select();
  try {
    await navigator.clipboard.writeText(data.value.link);
  } catch {
    document.execCommand("copy"); // no clipboard API (plain HTTP): the selected field copied the old way
  }
  copied.value = true;
}
</script>

<template>
  <PageState v-if="!data || status === 403" :status="status" :loading="loading" />
  <template v-else>
    <div class="head"><div>
      <h1>Comptes</h1>
      <p class="muted">Qui peut ouvrir ces pages. Un membre lit tout et écrit à Tarik dans Discussions ; un administrateur change aussi ce que Tarik utilise (contenu, réglages des cabinets, disposition des schémas) et gère les comptes. Un nouveau compte reçoit un lien à usage unique pour choisir son mot de passe ({{ data.hours }} h).</p>
    </div></div>
    <div v-if="data.error" class="panel bad" role="alert"><b>Non fait :</b> {{ data.error }}.</div>
    <div v-if="data.done" class="panel accent" role="status">{{ data.done }}</div>
    <div v-if="data.link" class="panel accent link-once" role="status">
      <p><b>Lien à envoyer à la personne</b>, valable {{ data.hours }} h et une seule fois. Il n'est montré qu'ici : copiez-le maintenant.</p>
      <div class="copy-row">
        <input id="invitation-link" ref="linkField" class="mono" :value="data.link" readonly>
        <button type="button" class="primary" @click="copy">{{ copied ? "Copié" : "Copier" }}</button>
      </div>
    </div>
    <section class="panel">
      <h2>Ajouter un compte</h2>
      <form class="add-account" @submit.prevent="act({ action: 'add', ...added })">
        <label class="field">
          <span>Email</span>
          <input v-model="added.email" name="email" type="email" required autocomplete="off">
        </label>
        <label class="field">
          <span>Nom</span>
          <input v-model="added.name" name="name" required autocomplete="off">
        </label>
        <label class="field">
          <span>Rôle</span>
          <select v-model="added.role" name="role">
            <option value="member">Membre</option>
            <option value="admin">Administrateur</option>
          </select>
        </label>
        <button type="submit" class="primary">Créer et obtenir le lien</button>
      </form>
    </section>
    <div class="table-wrap">
      <table class="runs accounts">
        <thead><tr><th>Compte</th><th>Rôle</th><th>État</th><th>Actions</th></tr></thead>
        <tbody>
          <tr v-for="a in data.accounts" :key="a.email">
            <td>
              <b>{{ a.name }}</b><template v-if="a.email === data.me"> <span class="muted">(vous)</span></template><br>
              <span class="mono muted">{{ a.email }}</span>
            </td>
            <td>{{ roleName(a.role) }}</td>
            <td>
              <span v-if="!a.active" class="chip">Fermé</span>
              <span v-else-if="!a.has_password" class="chip wait">Invité, sans mot de passe</span>
              <span v-else class="chip ok">Actif</span>
            </td>
            <td class="actions">
              <button
                v-if="a.active"
                type="button"
                class="panel-button"
                @click="act({ action: 'role', email: a.email, role: otherRole(a) })"
              >{{ roleButton(a) }}</button>
              <button type="button" class="panel-button" @click="act({ action: 'link', email: a.email })">
                {{ a.active ? "Nouveau lien" : "Rouvrir avec un lien" }}
              </button>
              <button
                v-if="a.active"
                type="button"
                class="panel-button"
                @click="act({ action: 'close', email: a.email }, `Fermer le compte de ${a.email} ? Ses sessions prennent fin.`)"
              >Fermer</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </template>
</template>
