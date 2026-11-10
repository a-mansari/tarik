# Tarik design system

The UI is a working tool for an accounting team: dense, calm, readable at a glance. One brand colour (green) marks what you can act on and where you are; green, amber and red mean status only.

- `tokens.css`: every colour, size, radius, shadow and duration. The only file where a raw value is chosen.
- `components.css`: button, field, switch, chip, panel, table. Built from tokens only.
- `../../design.html`: the living reference (`npm run dev`, then `/ui/design.html`). Not part of the build, no login.

## Rules

1. **No raw values in a page.** Colours, text sizes, radii, shadows and focus rings come from `var(--...)`. A missing token is added to `tokens.css` and shown in `design.html`, not written inline. `tests/style/test_design_system.py` fails on a raw one, an old class name, or a button without `.btn`.
2. **Roles, not shades.** Name a colour by what it is for (`--accent`, `--bad-bg`), never by how it looks. Each is one `light-dark(light, dark)` pair, so there is no second dark block to keep in step.
3. **One height, one look.** Every button and field is `--control-h`, and every text input, select and textarea is styled by `components.css` itself: a page never restyles a control. Icon-only buttons keep it, with a `title` and an `aria-label`.
4. **One primary action per view** (`.btn-primary`). A tinted choice beside it is `.btn-soft`, quiet ones `.btn` or `.btn-ghost`, destructive ones `.btn-danger` (and they confirm).
5. **Borders, not shadows.** A shadow only for what floats (`--shadow-pop`: popups, `--shadow-float`: the back-to-top button).
6. **Status is colour plus words or an icon**, never colour alone: `.chip-good`, `.chip-wait`, `.chip-bad`, `.panel-good|wait|bad` for notices.
7. **Little text.** A label says what the thing is. Explanations go in a `title`, not under every field. Use `.field-hint` only when the label cannot be clear.
8. **Space on the 4 px rhythm**: `--space-N` is N × 4 px (1, 2, 3, 4, 5, 6, 8, 12), and gap, padding and margin use them; radius by role: `--radius` controls, `--radius-lg` containers, `--radius-xl` popups, `--radius-pill` chips.

## Field grid

Settings-style forms put their fields in `.field-grid`: one to four per line, each at least `--field-min` and at most `--field-max` wide, so a field wraps below when the line is too narrow and never fills the whole line.

## Status classes

A run's or conversation's status is also a chip class (`.chip.done`, `.chip.wait`, `.chip.error`, ...): the status names come from the server, so `StatusChip` and the tables set them as they are. Anything not driven by a status uses `.chip-good|wait|bad|accent`.
