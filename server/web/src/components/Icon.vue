<!-- The pages' small line icons (24 px grid, drawn in the text's colour). -->
<script setup>
defineProps({ name: { type: String, required: true }, size: { type: Number, default: 16 } });
</script>

<script>
const PATHS = {
  runs: `<path d="M3 12h4l3-8 4 16 3-8h4"/>`,
  chat: `<path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/>`,
  workflows: `<rect x="3" y="3" width="7" height="6" rx="1.5"/><rect x="14" y="15" width="7" height="6" rx="1.5"/><path d="M6.5 9v3a3 3 0 0 0 3 3h4.5"/>`,
  network: `<circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="19" r="2.5"/><circle cx="19" cy="19" r="2.5"/><path d="M12 7.5v4M12 11.5l-5.5 5.5M12 11.5l5.5 5.5"/>`,
  prompts: `<path d="M4 17l6-5-6-5M12 19h8"/>`,
  content: `<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>`,
  journal: `<path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z"/><path d="M9 8h6M9 12h6M9 16h3"/>`,
  accounts: `<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.5a3.5 3.5 0 0 1 0 7M18.5 14.5A6.5 6.5 0 0 1 21.5 20"/>`,
  settings: `<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>`,
  download: `<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>`,
  attach: `<path d="M21 11.5l-8.5 8.5a5 5 0 0 1-7-7L14 4.5a3.3 3.3 0 0 1 4.7 4.7L10.2 17.7a1.7 1.7 0 0 1-2.4-2.4L15.5 7.6"/>`,
  file: `<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6"/>`,
  next: `<path d="M9 6l6 6-6 6"/>`,
  up: `<path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>`,
  down: `<path d="M15 10l5 5-5 5"/><path d="M4 4v7a4 4 0 0 0 4 4h12"/>`,
  home: `<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>`,
  mail: `<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>`,
  top: `<path d="M12 19V5M5 12l7-7 7 7"/>`,
  plus: `<path d="M12 5v14M5 12h14"/>`,
  clock: `<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>`,
  stop: `<rect x="6" y="6" width="12" height="12" rx="2"/>`,
  eye: `<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>`,
  toggle: `<rect x="2" y="7" width="20" height="10" rx="5"/><circle cx="16" cy="12" r="3"/>`,
  // States and actions drawn instead of worded (StatusChip.vue and the buttons' tooltips).
  power: `<path d="M12 3v9"/><path d="M6.4 6.4a8 8 0 1 0 11.2 0"/>`,
  check: `<path d="M5 12.5l4.5 4.5L19 7.5"/>`,
  "check-all": `<path d="M2 12.5l4.5 4.5L15 8"/><path d="M11 15l1.5 1.5L22 7.5"/>`,
  alert: `<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5M12 16.5v.01"/>`,
  running: `<circle cx="12" cy="12" r="9"/><path d="M10 8.5l5 3.5-5 3.5z"/>`,
  help: `<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.4-1 .9-1 1.7M12 17v.01"/>`,
  logout: `<path d="M9 4H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h4"/><path d="M16 8l4 4-4 4M20 12H9"/>`,
  trash: `<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>`,
  copy: `<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V6a2 2 0 0 1 2-2h8"/>`,
  send: `<path d="M21 3L10 14M21 3l-7 18-4-7-7-4z"/>`,
  flask: `<path d="M9 3h6M10 3v6l-5.5 9.5A1.5 1.5 0 0 0 5.8 21h12.4a1.5 1.5 0 0 0 1.3-2.5L14 9V3"/><path d="M7.5 15h9"/>`,
  unfold: `<path d="M7 9l5-5 5 5M7 15l5 5 5-5"/>`,
  fold: `<path d="M7 5l5 5 5-5M7 19l5-5 5 5"/>`,
  ban: `<circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/>`,
  // A canvas's zoom bar and its gestures (ZoomBar.vue, the legends of CanvasGraph.vue and HarnessMap.vue).
  fit: `<path d="M4 9V5a1 1 0 0 1 1-1h4M15 4h4a1 1 0 0 1 1 1v4M20 15v4a1 1 0 0 1-1 1h-4M9 20H5a1 1 0 0 1-1-1v-4"/><rect x="9" y="9" width="6" height="6" rx="1"/>`,
  "zoom-out": `<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4M8 11h6"/>`,
  "zoom-in": `<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4M8 11h6M11 8v6"/>`,
  click: `<path d="M9 9l6 12 2-5 5-2z"/><path d="M5 3v3M3 5h3M9 2v2M2 9h2"/>`,
  drag: `<path d="M12 2v20M2 12h20M12 2l-3 3M12 2l3 3M12 22l-3-3M12 22l3-3M2 12l3-3M2 12l3 3M22 12l-3-3M22 12l-3 3"/>`,
  wheel: `<rect x="6" y="2" width="12" height="20" rx="6"/><path d="M12 6v4"/>`,
};
</script>

<template>
  <svg class="icon" :width="size" :height="size" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" v-html="PATHS[name] || ''"></svg>
</template>
