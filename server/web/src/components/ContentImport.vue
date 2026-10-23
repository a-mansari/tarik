<!-- An import button: picks an exported file (JSON), sends it to ``to`` with its file's name (kept in the history's
note), and tells the page what came of it (done: {text, bad, saved}). Inside a card's summary it never folds it. -->
<script setup>
import { send } from "../api.js";

const props = defineProps({ to: { type: String, required: true }, tip: { type: String, default: "" } });
const emit = defineEmits(["done"]);

function pick() {
  const picker = Object.assign(document.createElement("input"), { type: "file", accept: "application/json,.json" });
  picker.addEventListener("change", async () => {
    const file = picker.files[0];
    if (!file) return;
    let data;
    try {
      data = JSON.parse(await file.text());
    } catch {
      return emit("done", { text: "Ce fichier n'est pas un export (JSON illisible).", bad: true });
    }
    const answer = await send(props.to, { ...data, file: file.name });
    if (!answer.ok) {
      const error = answer.data?.error || `erreur ${answer.status}`;
      return emit("done", { text: `Import refusé, rien n'a été enregistré : ${error}`, bad: true });
    }
    // One document's import answers saved: true; a whole export's, the documents saved.
    const saved = answer.data.saved === true ? 1 : (answer.data.saved || []).length;
    const text = saved
      ? `Importé : ${saved} nouvelle(s) version(s). Rechargement…`
      : "Rien n'a changé : le contenu était déjà celui-là.";
    emit("done", { text, saved });
  });
  picker.click();
}
</script>

<template>
  <button type="button" :data-tip="tip" :aria-description="tip" @click.prevent.stop="pick"><slot /></button>
</template>
