// The Cabinets page: switching a cabinet to real writes in Experio is confirmed first.
(() => {
  const form = document.getElementById("settings-form");
  if (!form) return;
  form.addEventListener("submit", (event) => {
    const flag = form.querySelector('select[name="DRY_RUN"]');
    if (flag && flag.dataset.was !== "0" && flag.value === "0"
      && !confirm("Tarik écrira réellement dans l'Experio de ce cabinet (brouillons, comptabilisations, dossiers). Continuer ?")) {
      event.preventDefault();
    }
  });
})();
