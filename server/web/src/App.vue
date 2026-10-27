<!-- The frame of every page once logged in: the sidebar (general pages, the cabinets), the cabinet's pages, who is
on them. The login and the invitation have the brand alone. -->
<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";

import { loadSession, send, session } from "./api.js";
import CabinetLabel from "./components/CabinetLabel.vue";
import Icon from "./components/Icon.vue";

const route = useRoute();
const router = useRouter();
const signed = computed(() => Boolean(session.user) && !route.meta.open);

// The sidebar's general pages and the cabinet's pages; an admin's own pages at the end of each.
const general = computed(() => {
  const links = [
    { href: "/ui/workflows", icon: "workflows", words: "Workflows" },
    { href: "/ui/tarik-prompts", icon: "content", words: "Tarik prompts" },
  ];
  if (session.admin) {
    links.push(
      { href: "/ui/comptes", icon: "accounts", words: "Comptes" },
      { href: "/ui/logs-serveur", icon: "journal", words: "Logs serveur" },
    );
  }
  return links;
});
const cabinetPages = computed(() => {
  const links = [
    { href: "/ui/accueil", icon: "home", words: "Accueil" },
    { href: "/ui/executions", icon: "runs", words: "Exécutions" },
    { href: "/ui/discussion", icon: "chat", words: "Discussions" },
    { href: "/ui/llm-calls", icon: "prompts", words: "LLM Calls" },
  ];
  if (session.admin) {
    links.push(
      { href: "/ui/routines", icon: "clock", words: "Routines" },
      { href: "/ui/settings", icon: "settings", words: "Réglages" },
    );
  }
  return links;
});
const current = (href) => route.path.startsWith(href);

// A cabinet picked in the sidebar: kept by the server (a cookie), then its dashboard, every page reading it again.
const pageKey = ref(0);
async function choose(name) {
  await send("/cabinet", { cabinet: name });
  await loadSession();
  pageKey.value += 1;
  router.push("/ui/accueil");
}

async function logout() {
  await send("/logout", {});
  location.assign("/login");
}

// Back to the top of the page, offered once the page is scrolled down.
const TOP_OFFERED_PX = 400; // scrolled this far down, the button shows
const scrolled = ref(false);
function onScroll() {
  scrolled.value = window.scrollY >= TOP_OFFERED_PX;
}
onMounted(() => window.addEventListener("scroll", onScroll, { passive: true }));
onUnmounted(() => window.removeEventListener("scroll", onScroll));
function toTop() {
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  window.scrollTo({ top: 0, behavior: still ? "auto" : "smooth" });
}

// On a phone the pages' menu scrolls sideways: it opens on the page shown.
const menu = ref(null);
const MENU_GUTTER_PX = 16; // left showing before the page shown
watch(() => route.path, async () => {
  await nextTick();
  const shown = menu.value?.querySelector("a[aria-current='page']");
  if (shown) menu.value.scrollLeft = shown.offsetLeft - menu.value.offsetLeft - MENU_GUTTER_PX;
}, { immediate: true });
</script>

<template>
  <div v-if="signed" class="frame">
    <aside class="sidebar" aria-label="Navigation">
      <RouterLink class="brand" to="/ui/accueil">Tarik</RouterLink>
      <section>
        <h2>Général</h2>
        <nav>
          <RouterLink v-for="link in general" :key="link.href" :to="link.href"
            :aria-current="current(link.href) ? 'page' : null"><Icon :name="link.icon" />{{ link.words }}</RouterLink>
        </nav>
      </section>
      <section>
        <h2>Cabinets</h2>
        <ul class="cabinets">
          <li v-for="cabinet in session.cabinets" :key="cabinet.name">
            <button type="button" :aria-current="cabinet.chosen ? 'true' : null" @click="choose(cabinet.name)">
              <CabinetLabel :name="cabinet.name" />
            </button>
          </li>
          <li v-if="!session.cabinets.length" class="muted">Aucun cabinet.</li>
        </ul>
      </section>
    </aside>
    <div class="column">
      <header class="bar">
        <!-- The pages of the cabinet picked in the sidebar: what differs from one cabinet to the next. -->
        <nav ref="menu" aria-label="Pages du cabinet">
          <RouterLink v-for="link in cabinetPages" :key="link.href" :to="link.href"
            :aria-current="current(link.href) ? 'page' : null"><Icon :name="link.icon" />{{ link.words }}</RouterLink>
        </nav>
        <form class="who" @submit.prevent="logout">
          <span>{{ session.user.name }}</span><button type="submit">Se déconnecter</button>
        </form>
      </header>
      <main class="page" :class="route.meta.pageClass">
        <RouterView :key="`${pageKey}:${route.path}`" />
      </main>
    </div>
  </div>
  <template v-else>
    <header class="bar"><a class="brand" href="/ui/accueil">Tarik</a></header>
    <main class="page"><RouterView /></main>
  </template>
  <button type="button" class="to-top" :hidden="!scrolled" aria-label="Revenir en haut de la page" @click="toTop">
    <Icon name="top" :size="18" />
  </button>
</template>
