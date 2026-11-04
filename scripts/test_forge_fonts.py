"""Static tests for the Realliance Forge typography (cluster/forgejo/custom/FONTS.md).

    python -m pytest -q -p no:cacheprovider scripts/test_forge_fonts.py

Runs as the `forge-fonts` pre-commit hook. The ConfigMaps are rendered with
`kustomize build cluster/forgejo`, the same build Argo runs. Checks:

  * no Google Fonts / CDN reference anywhere in the forge customisation;
  * every @font-face src resolves to a file that ships in a ConfigMap, mounted where the
    stylesheet expects it;
  * every ConfigMap is under 1 MiB (the Kubernetes cap) and under the 256 KB last-applied
    limit unless it carries the ServerSideApply annotation;
  * fonts apply to every selectable theme: one stylesheet, loaded by custom/header.tmpl,
    and none of the theme sheets sets a font-family of its own.

It opens only the paths named here and never reads `*.secret.*` files.
"""

import base64
import posixpath
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
CUSTOM = REPO / "cluster/forgejo/custom"
CSS = CUSTOM / "public/assets/css/realliance-fonts.css"
HEADER = CUSTOM / "templates/custom/header.tmpl"
RELEASE = REPO / "cluster/applications/forgejo-release.yaml"

MIB = 1024 * 1024
LAST_APPLIED_CAP = 256 * 1024
CDN_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com", "googleapis.com", "gstatic.com", "cdnjs", "jsdelivr", "unpkg.com", "bunny.net", "typekit")

# Files that make up the customisation, besides the Catppuccin sheets (checked separately).
OWN_TEXT_FILES = [CSS, HEADER, CUSTOM / "FONTS.md", REPO / "scripts/build-forge-fonts.sh"]


@pytest.fixture(scope="module")
def rendered():
    if shutil.which("kustomize") is None:
        pytest.skip("kustomize not on PATH (run inside `nix develop`)")
    out = subprocess.run(["kustomize", "build", str(REPO / "cluster/forgejo")], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


@pytest.fixture(scope="module")
def configmaps(rendered):
    return {d["metadata"]["name"]: d for d in rendered if d["kind"] == "ConfigMap"}


def cm_size(cm) -> int:
    """Bytes the API server counts: decoded data plus decoded binaryData values."""
    n = sum(len(v.encode()) for v in (cm.get("data") or {}).values())
    n += sum(len(base64.b64decode(v)) for v in (cm.get("binaryData") or {}).values())
    return n


def cm_files(cm) -> dict:
    return {**(cm.get("data") or {}), **{k: None for k in (cm.get("binaryData") or {})}}


def test_no_cdn_in_forge_customisation():
    files = [p for p in CUSTOM.rglob("*") if p.is_file() and p.suffix in {".css", ".tmpl", ".md", ".txt", ".html"}]
    files += [p for p in OWN_TEXT_FILES if p.is_file()]
    assert CSS.is_file() and HEADER.is_file(), "stylesheet and header template must exist"
    for p in set(files):
        if p.name == "FONTS.md" or p.name == "build-forge-fonts.sh":
            continue  # these name the hosts only to say we never use them
        text = p.read_text(errors="replace").lower()
        for host in CDN_HOSTS:
            assert host not in text, f"{p.relative_to(REPO)} references {host}"
    # the Helm release and the kustomization, too
    for p in (RELEASE, REPO / "cluster/forgejo/kustomization.yaml"):
        text = p.read_text().lower()
        assert "fonts.googleapis.com" not in text and "fonts.gstatic.com" not in text, p
    # and no absolute URL at all in the stylesheet or the header template
    for p in (CSS, HEADER):
        assert not re.search(r"https?://|//[a-z0-9.-]+\.[a-z]{2,}/", p.read_text()), f"{p.name} has an absolute URL"


def test_every_font_face_src_ships(configmaps):
    css = CSS.read_text()
    faces = re.findall(r"@font-face\s*\{[^}]*\}", css)
    assert len(faces) >= 6, "expected Atkinson x4, Fredoka, Noto Sans Mono"
    shipped = set(cm_files(configmaps["forgejo-fonts"]))
    # mount points: css -> /data/gitea/public/assets/css, fonts -> .../assets/fonts (forgejo-release.yaml)
    release = yaml.safe_load(RELEASE.read_text())
    values = release["spec"]["source"]["helm"]["valuesObject"]
    mounts = {m["name"]: m["mountPath"] for m in values["extraContainerVolumeMounts"] if "subPath" not in m}
    assert mounts["fonts"] == "/data/gitea/public/assets/fonts"
    assert mounts["themes"] == "/data/gitea/public/assets/css"
    vols = {v["name"]: v for v in values["extraVolumes"]}
    assert vols["fonts"]["configMap"]["name"] == "forgejo-fonts"
    assert {"configMap": {"name": "forgejo-font-css"}} in vols["themes"]["projected"]["sources"]
    assert "realliance-fonts.css" in cm_files(configmaps["forgejo-font-css"])
    css_dir = "/data/gitea/public/assets/css"
    for face in faces:
        assert "font-display:swap" in face.replace(" ", ""), face
        for url in re.findall(r"url\(\s*[\"']?([^\"')]+)", face):
            assert "format(\"woff2\")" in face
            target = Path(posixpath.normpath(f"{css_dir}/{url}"))
            assert str(target.parent) == mounts["fonts"], f"{url} resolves outside the fonts mount: {target}"
            assert target.name in shipped, f"{url} is not in ConfigMap forgejo-fonts"
            blob = base64.b64decode(configmaps["forgejo-fonts"]["binaryData"][target.name])
            assert blob[:4] == b"wOF2", f"{target.name} is not woff2"


def test_preload_targets_a_shipped_body_font(configmaps):
    text = HEADER.read_text()
    m = re.search(r'rel="preload" href="\{\{AssetUrlPrefix\}\}/fonts/([^"]+)" as="font" type="font/woff2" crossorigin', text)
    assert m, "header.tmpl must preload the body font (with crossorigin)"
    assert m.group(1) in cm_files(configmaps["forgejo-fonts"])
    assert m.group(1).startswith("atkinson-hyperlegible-Regular")
    assert "{{AssetUrlPrefix}}/css/realliance-fonts.css" in text


def test_header_template_is_mounted():
    values = yaml.safe_load(RELEASE.read_text())["spec"]["source"]["helm"]["valuesObject"]
    mount = [m for m in values["extraContainerVolumeMounts"] if m.get("subPath") == "custom-header.tmpl"]
    assert [m["mountPath"] for m in mount] == ["/data/gitea/templates/custom/header.tmpl"]
    kust = yaml.safe_load((REPO / "cluster/forgejo/kustomization.yaml").read_text())
    gen = {g["name"]: g for g in kust["configMapGenerator"]}
    assert any(f.startswith("custom-header.tmpl=") for f in gen["forgejo-custom-templates"]["files"])


def test_every_configmap_under_1mib(configmaps):
    assert {"forgejo-fonts", "forgejo-font-css", "forgejo-themes-1", "forgejo-themes-2", "forgejo-custom-templates"} <= set(configmaps)
    for name, cm in configmaps.items():
        size = cm_size(cm)
        assert size < MIB, f"ConfigMap {name} is {size} bytes (cap {MIB})"
        ssa = cm["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-options", "")
        if size > LAST_APPLIED_CAP // 2:
            assert "ServerSideApply=true" in ssa, f"{name} ({size} B) needs ServerSideApply"


def test_stylesheet_overrides_only_font_variables():
    css = CSS.read_text()
    assert re.search(r"--fonts-proportional:\s*\"Atkinson Hyperlegible\",", css)
    assert re.search(r"--fonts-monospace:\s*\"Noto Sans Mono\",", css)
    assert re.search(r"--fonts-heading:\s*\"Fredoka\",", css)
    # fallbacks kept: the upstream stacks follow our face
    assert "system-ui" in css and "monospace" in css and "sans-serif" in css
    assert re.search(r"h1,h2,h3,h4,h5,h6,\.ui\.header\{font-family:var\(--fonts-heading\)\}", css)


def test_fonts_apply_to_every_selectable_theme():
    """One sheet, loaded after the theme sheet on every page; no theme sets a face of its own."""
    values = yaml.safe_load(RELEASE.read_text())["spec"]["source"]["helm"]["valuesObject"]
    themes = [t.strip() for t in values["gitea"]["config"]["ui"]["THEMES"].split(",")]
    assert len(themes) > 1
    # themes that ship as files are ours (Catppuccin); the built-in ones (forgejo-*, gitea-*) are
    # Forgejo's own and only set colours.
    css_dir = CUSTOM / "public/assets/css"
    for t in themes:
        if t.startswith("catppuccin-"):
            sheet = css_dir / f"theme-{t}.css"
            assert sheet.is_file(), t
            text = sheet.read_text()
            assert "font-family" not in text and "--fonts-" not in text, f"{sheet.name} sets a font"
            assert "@font-face" not in text
    assert values["gitea"]["config"]["ui"]["DEFAULT_THEME"] in themes
