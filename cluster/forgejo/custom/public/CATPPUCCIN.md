# Catppuccin themes for Forgejo

`assets/css/theme-catppuccin-*.css` are copied unmodified from
<https://github.com/catppuccin/gitea> release **v1.0.2** (`catppuccin-gitea.tar.gz`,
sha256 `1cfe00a789762b50563f5cd67422984b963937e25c2807178be1f1d60e8c5707`), MIT licence,
© Catppuccin. They ship as ConfigMaps `forgejo-themes-1` (latte, frappé) and `forgejo-themes-2`
(macchiato, mocha, auto) — split because one ConfigMap is limited to 1 MiB — merged into one
projected volume at `$GITEA_CUSTOM/public/assets/css`, and listed in `[ui] THEMES`.

To update: download the new release, check its sha256, replace the files, keep the kustomize
file lists and `[ui] THEMES` in step (new accents/flavours), bump `realliance/templates-rev`.
