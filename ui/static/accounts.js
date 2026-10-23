// The Comptes page: a new link copied in one click, and closing an account confirmed first.
(() => {
  for (const button of document.querySelectorAll("[data-copy]")) {
    button.addEventListener("click", async () => {
      const field = document.getElementById(button.dataset.copy);
      field.select();
      try { await navigator.clipboard.writeText(field.value); } catch { document.execCommand("copy"); }
      button.textContent = "Copié";
    });
  }
  for (const form of document.querySelectorAll("form[data-confirm]")) {
    form.addEventListener("submit", (event) => { if (!confirm(form.dataset.confirm)) event.preventDefault(); });
  }
})();
