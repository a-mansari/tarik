import { createApp } from "vue";

import App from "./App.vue";
import { loadSession } from "./api.js";
import { router } from "./router.js";
import "./app.css";

await loadSession(); // signed out: the pages that need a session send it to the login
createApp(App).use(router).mount("#app");
