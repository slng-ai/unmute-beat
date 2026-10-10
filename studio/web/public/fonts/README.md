# Fonts

The studio uses SLNG's brand fonts: **Saans** for words people read and **Geist Mono** for anything the
engine produced. Geist Mono loads from Google Fonts (`index.html`).

Saans is licensed to SLNG and is not committed here. `npm run fonts` copies `Saans-Regular.woff2`,
`Saans-SemiBold.woff2` and `Saans-Heavy.woff2` from a checkout of `slng-ai/frontend` next to this
repository (`../frontend/libs/ui/src/assets/fonts`, or the folder in `SLNG_FONTS`). Without them the
page falls back to the system sans-serif, and nothing else changes.
