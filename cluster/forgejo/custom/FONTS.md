# Realliance Forge typography

Decided by Chris 2026-11-04. All three families are SIL OFL 1.1, **self-hosted**: nothing is ever loaded
from Google Fonts or any CDN (`scripts/test_forge_fonts.py` fails the build if a CDN host appears).

| Role | Family | Files (`public/assets/fonts/`) | Weights |
|------|--------|-------------------------------|---------|
| Body | Atkinson Hyperlegible | `atkinson-hyperlegible-{Regular,Italic,Bold,BoldItalic}.woff2` | 400, 700, + italics |
| Headings (`h1`-`h4`, markdown `h1`-`h6`, repo title; not `.ui.header`) | Fredoka | `fredoka-latin.woff2` | variable 300-700 |
| Code (code blocks, diffs, file views, inline code) | Noto Sans Mono | `noto-sans-mono-latin.woff2` | variable 100-900 |

Subset: **latin only** (Basic Latin, Latin-1, general punctuation, euro/trademark/arrows/minus; no latin-ext).
The width axis of Fredoka and Noto Sans Mono is pinned at 100 (the weight axis is kept). Fallback stacks are
Forgejo's own, after our face. `font-display: swap`.

## How it ships

- `public/assets/css/realliance-fonts.css`: the `@font-face` rules, plus the replacement of Forgejo's
  `--fonts-proportional` / `--fonts-monospace` variables and a `--fonts-heading` for headings.
  ConfigMap `forgejo-font-css`, a third source of the projected `themes` volume (`assets/css/`).
- `public/assets/fonts/*`: ConfigMap `forgejo-fonts` (binaryData, ~0.2 MiB), mounted at `assets/fonts/`.
  The three `OFL-*.txt` licences ship in the same directory.
- `templates/custom/header.tmpl`: Forgejo's `<head>` hook, rendered after the theme sheet on every page, so
  one stylesheet covers every selectable theme. Preloads the Regular body face.
- Wiring: `forgejo-release.yaml` (volume `fonts`, mounts) and `kustomization.yaml` (generators).
  Adding the volume, mounts and template changes the pod spec and `realliance/templates-rev`, so Forgejo restarts once.

## Sources and rebuild

The fonts come from the brand kit, **git.realliance.net/realliance/brand** (`fonts/sources.json`: google/fonts
commit `2fe1e8c6b137fb13041a7cfca6fdcd8a910ea4dc`, every source pinned by sha256, fetched by Nix; byte-reproducible
since `head.modified` is kept from the source). Never edit the woff2 files here: they are copies of the brand's
`dist/fonts/`, under its content-hashed names (`fredoka-latin.<8 hex>.woff2`), so every change gets a new URL.

```bash
scripts/sync-brand.sh v0.1.0 [--rebuild]   # verify the signed tag, copy, re-point, prune
```

It also rewrites the `url()`s in `realliance-fonts.css` (the source; edit this one) and ships it as the hashed
copy `realliance-fonts.<8 hex>.css`, which the `<head>` hook and the `forgejo-font-css` ConfigMap name. After
editing `realliance-fonts.css` by hand, re-run the sync with the current tag (`brand.lock.json`) to re-hash it;
`scripts/test_forge_fonts.py` fails until you do. The sync records the release in `brand.lock.json` and bumps
`realliance/templates-rev` whenever it changes something, because the `<head>` hook is read at start.

## Upgrades

Forgejo changes `--fonts-*` rarely, but after a Forgejo upgrade check `index.css` still defines
`--fonts-proportional` and `--fonts-monospace` and that `templates/custom/header.tmpl` is still rendered
(`templates/base/head.tmpl`).
