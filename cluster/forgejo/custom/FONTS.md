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

Source of every font: <https://github.com/google/fonts> at commit
`2fe1e8c6b137fb13041a7cfca6fdcd8a910ea4dc` (2026-11-03), under `ofl/`. sha256 of each source file:

| Source | sha256 |
|--------|--------|
| `atkinsonhyperlegible/AtkinsonHyperlegible-Regular.ttf` | `7fb917c89019896d0b52ee84b7cbb3304c18cb90b19a62f5e32712bd23e97669` |
| `atkinsonhyperlegible/AtkinsonHyperlegible-Italic.ttf` | `021beda4d3c6edfc78872e436d74009f9a1bcb294331908fe5747c61d3dcc514` |
| `atkinsonhyperlegible/AtkinsonHyperlegible-Bold.ttf` | `5a3b0c8cc8ca545155150b4512a1fa248298df121c50d6557e651e61fbdab92f` |
| `atkinsonhyperlegible/AtkinsonHyperlegible-BoldItalic.ttf` | `7a46f1b0ef2f64b61065c1a6b5a11fcb9b019b44062d88ba121a083cc39e86bf` |
| `atkinsonhyperlegible/OFL.txt` | `f32d22b3908fcad2c86a74000614ec22e6a7f66ea7e867e616026a27aebdc143` |
| `fredoka/Fredoka[wdth,wght].ttf` | `2ba02e68b152868aef9ba28e24b3648c7d457fe6f25c761f2c2c53fb61a73fc8` |
| `fredoka/OFL.txt` | `5c9e7eee5c6b25f4b05b8d53b2e470ea4962f9ced742d044a98f7d95d1375bab` |
| `notosansmono/NotoSansMono[wdth,wght].ttf` | `2cb2adb378a8f574213e23df697050b83c54c27df465a2015552740b2769a081` |
| `notosansmono/OFL.txt` | `cee9892f9f0cc8fe882c9e9537ee6a89621d86ee7ceaf70b02e2b2b1c25c061a` |

`scripts/build-forge-fonts.sh` re-downloads into a fresh temp dir, verifies those hashes, and runs
`pyftsubset --unicodes=<latin set> --flavor=woff2 --layout-features='*'` (static Atkinson) and
`fonttools varLib.instancer <ttf> wdth=100` then `pyftsubset` (variable Fredoka, Noto Sans Mono),
through `nix shell` with fonttools + brotli. The variable fonts are not byte-reproducible (the instancer
stamps `head.modified`); the glyph set and axes are. The committed woff2 files are authoritative.

## Upgrades

Forgejo changes `--fonts-*` rarely, but after a Forgejo upgrade check `index.css` still defines
`--fonts-proportional` and `--fonts-monospace` and that `templates/custom/header.tmpl` is still rendered
(`templates/base/head.tmpl`).
