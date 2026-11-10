# Tarik design system

The UI is a working tool for an accounting team: dense, calm, readable at a glance. One brand colour (green) marks what you can act on and where you are; green, amber and red mean status only.

- `tokens.css`: every colour, size, radius, shadow and duration. The only file where a raw value is chosen.
- `components.css`: button, field, switch, chip, panel, table. Built from tokens only.
- `../../design.html`: the living reference (`npm run dev`, then `/ui/design.html`). Not part of the build, no login.

## Rules

1. **No raw values in a page.** Colours, sizes, radii and shadows come from `var(--...)`. A missing token is added to `tokens.css` and shown in `design.html`, not written inline.
2. **Roles, not shades.** Name a colour by what it is for (`--accent`, `--bad-bg`), never by how it looks. Each is one `light-dark(light, dark)` pair, so there is no second dark block to keep in step.
3. **One height.** Every button and field is `--control-h`. Icon-only buttons keep it, with a `title` and an `aria-label`.
4. **One primary action per view** (`.btn-primary`). A tinted choice beside it is `.btn-soft`, quiet ones `.btn` or `.btn-ghost`, destructive ones `.btn-danger` (and they confirm).
5. **Borders, not shadows.** A shadow only for what floats (`--shadow-pop`: popups, `--shadow-float`: the back-to-top button).
6. **Status is colour plus words or an icon**, never colour alone: `.chip-good`, `.chip-wait`, `.chip-bad`, `.panel-good|wait|bad` for notices.
7. **Little text.** A label says what the thing is. Explanations go in a `title`, not under every field. Use `.field-hint` only when the label cannot be clear.
8. **Space on the 4 px rhythm** (`--space-1` to `--space-7`); radius by role: `--radius` controls, `--radius-lg` containers, `--radius-xl` popups, `--radius-pill` chips.

## Old class names

Pages still using `.primary`, `.panel-button`, `.card-action`, `.panel.accent`, ... are styled by the same rules (they are listed beside the new names in `components.css`). Move a page to the new names when you touch it; once none uses an old name, delete it from the list.
