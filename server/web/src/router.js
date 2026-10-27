// The pages, at the addresses the server had them (server/server.py serves this app for each): /ui/... once logged in,
// /login and /invitation/... before. Each view reads its own data (api.js: usePage).
import { createRouter, createWebHistory } from "vue-router";

const view = (name) => () => import(`./views/${name}.vue`);

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/ui/accueil" },
    { path: "/ui", redirect: "/ui/accueil" },
    { path: "/login", component: view("Login"), meta: { open: true, title: "Connexion" } },
    { path: "/invitation/:token", component: view("Invitation"), meta: { open: true, title: "Invitation" } },
    { path: "/ui/accueil", component: view("Dashboard"), meta: { title: "Accueil" } },
    { path: "/ui/executions", component: view("Runs"), meta: { title: "Exécutions" } },
    { path: "/ui/executions/:run", component: view("Run"), meta: { title: "Exécution", pageClass: "wide" } },
    { path: "/ui/workflows", component: view("Workflows"), meta: { title: "Workflows" } },
    { path: "/ui/workflows/:graph", component: view("Workflow"), meta: { title: "Workflow", pageClass: "wide" } },
    { path: "/ui/discussion", component: view("Chat"), meta: { title: "Discussions" } },
    { path: "/ui/discussion/:session", component: view("Chat"), meta: { title: "Discussion" } },
    { path: "/ui/llm-calls", component: view("Prompts"), meta: { title: "LLM Calls" } },
    { path: "/ui/llm-calls/:session", component: view("PromptCalls"), meta: { title: "LLM Calls" } },
    { path: "/ui/tarik-prompts", component: view("Contents"), meta: { title: "Tarik prompts" } },
    { path: "/ui/tarik-prompts/:path(.*)", component: view("Content"), meta: { title: "Tarik prompts", pageClass: "wide" } },
    { path: "/ui/settings", component: view("Settings"), meta: { title: "Réglages", pageClass: "wide" } },
    { path: "/ui/routines", component: view("Routines"), meta: { title: "Routines" } },
    { path: "/ui/comptes", component: view("Accounts"), meta: { title: "Comptes" } },
    { path: "/ui/logs-serveur", component: view("Journal"), meta: { title: "Logs serveur" } },
    { path: "/:rest(.*)", component: view("Missing"), meta: { title: "Introuvable" } },
  ],
  scrollBehavior: (to, from, saved) => saved || { top: 0 },
});

router.afterEach((to) => {
  document.title = `${to.meta.title || "Tarik"} · Tarik`;
});
