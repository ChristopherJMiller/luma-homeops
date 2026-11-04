# Realliance Forge logo

Chosen by Chris 2026-11-04 (exploration: `.superpowers/logo/`). Full logo 2c (Fredoka 600, 7 degree tumble, three
rounded twinkles); navbar 3d (ReA + one twinkle); favicon 3f (R + twinkle on a rounded Mocha tile); sparkles `#df8e1d`.

**Source: the brand kit, git.realliance.net/realliance/brand** (Inkscape sources, palette, fonts; its `dist/` is
built and committed there). Never edit these files here: `scripts/sync-brand.sh <tag> [--rebuild]` verifies the
signed tag, copies what the forge uses, checks each file's sha256 against the brand's `dist/manifest.json`,
re-points the references, deletes stale copies and records the release in `brand.lock.json`.

Delivered like the fonts: ConfigMap `forgejo-img` mounted at `/data/gitea/public/assets/img` (new volume = one restart).

| File | From realliance/brand `dist/` | Used by |
|------|------|---------|
| `logo.svg` | `fixed/forgejo/logo.svg` (mark-wide, `-auto`) | navbar (30px tall), home page (220px), PWA manifest. Ink follows `prefers-color-scheme` |
| `logo-mocha.<hash>.svg`, `logo-latte.<hash>.svg` | `img/mark-wide-{mocha,latte}` | baked ink, swapped in by `realliance-fonts.css` for fixed themes |
| `logo.png` | `fixed/forgejo/logo.png` | og:image, PWA (512, the full 2c logo on a tile) |
| `favicon.svg`, `favicon.png`, `apple-touch-icon.png` | `fixed/forgejo/…` | `<head>`; `/favicon.ico` redirects to favicon.png |

**Names:** everything the stylesheet references carries a content hash, so a change is a new URL. The five names
Forgejo itself requests (`logo.svg`, `logo.png`, `favicon.svg`, `favicon.png`, `apple-touch-icon.png`) stay fixed:
Forgejo serves `/assets/*` with a 6 h max-age and no version on these, so after a change browsers may show the old
logo or favicon for up to 6 h (or until a hard reload). The forge is not behind Cloudflare.

Theme ink: `*-auto` themes use the SVG's own media query. Fixed themes (`data-theme` containing latte/light or
frappe/macchiato/mocha/dark) get `content:url()` on the img. `scripts/test_forge_fonts.py` checks the files exist,
contain no external reference, carry the hash of their content, and that nothing stale is left.
