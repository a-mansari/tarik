<!-- The message to Tarik and its files: on a conversation's page, or on the dashboard where it opens one. Its limits
(chat_pages.LIMITS) listed under it, and checked here before anything is sent, as the server checks them again
(chat_pages.problems). Files go as base64 in JSON. Enter sends, Shift+Enter goes to a new line, as in n8n's chat. -->
<script setup>
import { ref } from "vue";

import { send } from "../api.js";
import Icon from "./Icon.vue";

const props = defineProps({
  session: { type: String, required: true },
  cabinet: { type: String, required: true },
  checks: { type: Object, required: true },
  accepted: { type: String, required: true },
  limits: { type: Array, required: true },
  busy: { type: Boolean, default: false }, // Tarik answering: nothing more sent meanwhile
});
const emit = defineEmits(["sent"]);

const form = ref(null), box = ref(null), limitsOpen = ref(false);
const text = ref(""), files = ref([]), failure = ref(""), sending = ref(false);

defineExpose({ focus: () => box.value?.focus() });

const megabytes = (size) => `${(size / 1048576).toFixed(1).replace(".", ",")} Mo`;
// The caps the server holds (chat_pages.problems), said the same way before anything is read or sent.
function problems(message, chosen) {
  const { accepted, max_files, max_file, max_text, types } = props.checks;
  const found = [];
  if (message.length > max_text) found.push(`Message de ${message.length} caractères : ${max_text} au plus. Raccourcissez-le.`);
  if (chosen.length > max_files) found.push(`${chosen.length} fichiers joints : ${max_files} au plus par message. Envoyez les autres dans un second message, ou réunissez-les dans un zip.`);
  for (const file of chosen) {
    if (!accepted.some((kind) => file.name.toLowerCase().endsWith(kind))) found.push(`${file.name} : type non accepté. Types acceptés : ${types}.`);
    else if (!file.size) found.push(`${file.name} : fichier vide.`);
    else if (file.size > max_file) found.push(`${file.name} : ${megabytes(file.size)}, ${max_file / 1048576} Mo au plus par fichier. Envoyez-le compressé ou en plusieurs parties.`);
  }
  return found;
}

const base64 = (file) => new Promise((done, failed) => {
  const reader = new FileReader();
  reader.onload = () => done(reader.result.split(",")[1] || "");
  reader.onerror = () => failed(reader.error);
  reader.readAsDataURL(file);
});

async function submit() {
  failure.value = "";
  const exceeded = problems(text.value.trim(), files.value);
  if (exceeded.length) {
    failure.value = `Rien n'a été envoyé :\n${exceeded.map((line) => `- ${line}`).join("\n")}`;
    limitsOpen.value = true;
    return;
  }
  sending.value = true;
  try {
    const given = await Promise.all(files.value.map(async (file) => ({
      file_name: file.name, mime_type: file.type, data: await base64(file),
    })));
    const answer = await send(`/discussion/${encodeURIComponent(props.session)}`,
      { cabinet: props.cabinet, message: text.value, files: given });
    if (!answer.ok) throw new Error(answer.data?.error || "Message refusé.");
    if (answer.data.skipped?.length) { // what a zip left out, told to who sent it
      failure.value = `Fichiers laissés de côté :\n${answer.data.skipped.map((line) => `- ${line}`).join("\n")}`;
    }
    form.value.reset();
    text.value = "";
    files.value = [];
    emit("sent", answer.data);
  } catch (error) {
    failure.value = error.message;
  } finally {
    sending.value = false;
  }
}

function onKey(event) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.value.requestSubmit();
  }
}
</script>

<template>
  <form ref="form" class="chat-form" @submit.prevent="submit">
    <textarea ref="box" v-model="text" name="message" rows="3" :maxlength="checks.max_text" :disabled="busy || sending"
      placeholder="Votre message à Tarik (le dossier, achats ou ventes…)" @keydown="onKey"></textarea>
    <div class="chat-actions">
      <label class="attach"><Icon name="attach" /><span>Joindre des fichiers</span>
        <input type="file" name="files" multiple :accept="accepted" @change="files = [...$event.target.files]"></label>
      <ul class="chosen" aria-live="polite"><li v-for="(file, at) in files" :key="at">{{ file.name }}</li></ul>
      <button type="submit" class="primary" :disabled="busy || sending">Envoyer</button>
    </div>
    <p v-if="failure" class="bad-text">{{ failure }}</p>
    <details class="limits" :open="limitsOpen" @toggle="limitsOpen = $event.target.open">
      <summary>Limites des fichiers</summary>
      <dl><template v-for="[what, cap] in limits" :key="what"><dt>{{ what }}</dt><dd>{{ cap }}</dd></template></dl>
    </details>
  </form>
</template>
