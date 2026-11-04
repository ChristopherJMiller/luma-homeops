# Realliance Forge logo

Chosen by Chris 2026-11-04 (exploration: `.superpowers/logo/`). Full logo 2c (Fredoka 600, 7 degree tumble, three
rounded twinkles); navbar 3d (ReA + one twinkle); favicon 3f (R + twinkle on a rounded Mocha tile); sparkles `#df8e1d`.

Delivered like the fonts: ConfigMap `forgejo-img` mounted at `/data/gitea/public/assets/img` (new volume = one restart).

| File | Used by |
|------|---------|
| `logo.svg` | navbar (30px tall), home page (220px), PWA manifest. 3d, ink follows `prefers-color-scheme` |
| `logo-mocha.svg`, `logo-latte.svg` | baked ink, swapped in by `realliance-fonts.css` for fixed themes |
| `logo.png` | og:image, PWA (512, the full 2c logo on a tile) |
| `favicon.svg`, `favicon.png`, `apple-touch-icon.png` | `<head>`; `/favicon.ico` redirects to favicon.png |

Theme ink: `*-auto` themes use the SVG's own media query. Fixed themes (`data-theme` containing latte/light or
frappe/macchiato/mocha/dark) get `content:url()` on the img. `scripts/test_forge_fonts.py` checks the files exist and
contain no external reference. Regenerate from the sources in the luma-homeops `.superpowers/logo/src/`.
