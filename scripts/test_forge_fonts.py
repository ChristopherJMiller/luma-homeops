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
    and none of the theme sheets sets a font-family of its own;
  * the realliance/brand sync (scripts/sync-brand.sh): every content-hashed file is named after its
    sha256, the shipped stylesheet is the hashed copy of realliance-fonts.css, and no stale copy is left.

It opens only the paths named here and never reads `*.secret.*` files.
"""

import base64
import hashlib
import json
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
LOCK = CUSTOM / "brand.lock.json"
HASHED = re.compile(r"^(?P<base>.+)\.(?P<h>[0-9a-f]{8})\.(?P<ext>css|woff2|svg|png)$")
RELEASE = REPO / "cluster/applications/forgejo-release.yaml"

MIB = 1024 * 1024
LAST_APPLIED_CAP = 256 * 1024
CDN_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com", "googleapis.com", "gstatic.com", "cdnjs", "jsdelivr", "unpkg.com", "bunny.net", "typekit")

# Files that make up the customisation, besides the Catppuccin sheets (checked separately).
OWN_TEXT_FILES = [CSS, HEADER, CUSTOM / "FONTS.md", REPO / "scripts/sync-brand.sh"]


def shipped_css_name() -> str:
    """The content-hashed copy of realliance-fonts.css that the <head> hook loads."""
    m = re.search(r'\{\{AssetUrlPrefix\}\}/css/(realliance-fonts\.[0-9a-f]{8}\.css)', HEADER.read_text())
    assert m, "header.tmpl must load the hashed realliance-fonts.<8 hex>.css"
    return m.group(1)


def hashed_logo(flavour: str) -> str:
    names = [p.name for p in (CUSTOM / "public/assets/img").glob(f"logo-{flavour}.*.svg")]
    assert len(names) == 1, f"want exactly one logo-{flavour}.<8 hex>.svg, found {names}"
    return names[0]


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
        if p.name == "FONTS.md":
            continue  # names the hosts only to say we never use them
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
    assert shipped_css_name() in cm_files(configmaps["forgejo-font-css"])
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
    assert shipped_css_name() in cm_files(configmaps["forgejo-font-css"])


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
    # Fredoka only on real headings; small Fomantic section titles (.ui.header) stay Atkinson
    assert ":where(h1,h2,h3,h4):not(.ui.header)" in css
    assert ".markup h1,.markup h2,.markup h3,.markup h4,.markup h5,.markup h6" in css
    assert ".repo-header .flex-item-title" in css
    assert ".ui.header{font-family:var(--fonts-regular)}" in css
    heading_rules = re.findall(r"([^{}]+)\{font-family:var\(--fonts-heading\)\}", css)
    assert heading_rules
    assert not any(re.search(r"(^|,)\s*\.ui\.header\s*(,|$)", r.strip()) for r in heading_rules)


def test_radius_variant_b_only_touches_the_two_radius_variables():
    css = CSS.read_text()
    assert ":root{--border-radius:8px;--border-radius-medium:12px}" in css
    declared = set(re.findall(r"(--[a-z-]+)\s*:", css))
    assert declared <= {"--fonts-proportional", "--fonts-monospace", "--fonts-heading",
                        "--border-radius", "--border-radius-medium"}, declared
    # avatars keep their old corners; pills (--border-radius-full) and counters are not touched
    assert re.search(r"\.ui\.avatar[^{]*\{border-radius:4px\}", css)
    assert "--border-radius-full" not in css and "--border-radius-large" not in css
    # radius rules never hide or move anything
    for bad in (r"display\s*:\s*none", r"visibility\s*:\s*hidden", r"position\s*:"):
        assert not re.search(bad, css), bad


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


def test_attached_segment_radius_does_not_square_the_last_child():
    # round 2 squared the bottom of every last-child attached segment (diff file bodies); stock rounds it
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(), flags=re.S)
    for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if "segment" in selectors and ":not(.top):not(.bottom)" in selectors:
            assert not re.search(r"border-radius\s*:\s*0\s*(;|$)", body.strip()), selectors
    assert ".ui.attached.segment:last-child:not(.top){border-radius:0 0 12px 12px}".replace(" ", "") in css.replace(" ", "")


# --- Realliance logo (cluster/forgejo/custom/BRAND.md) ---
IMG = CUSTOM / "public/assets/img"
FIXED_IMG = ["logo.svg", "logo.png", "favicon.svg", "favicon.png", "apple-touch-icon.png"]  # names Forgejo requests
IMG_FILES = FIXED_IMG + [hashed_logo("mocha"), hashed_logo("latte")]  # + the baked logos, content-hashed
PNG_SIZES = {"logo.png": (512, 512), "favicon.png": (180, 180), "apple-touch-icon.png": (180, 180)}


def test_logo_files_exist_and_are_wellformed():
    for name in IMG_FILES:
        assert (IMG / name).is_file(), f"missing {name}"
    for name, (w, h) in PNG_SIZES.items():
        b = (IMG / name).read_bytes()
        assert b[:8] == b"\x89PNG\r\n\x1a\n", name
        assert (int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")) == (w, h), name


def test_logo_svgs_have_no_external_references():
    for p in IMG.glob("*.svg"):
        t = p.read_text()
        assert "http" not in t.replace('xmlns="http://www.w3.org/2000/svg"', ""), f"{p.name} references a URL"
        assert not re.search(r"<(image|use|script|foreignObject)\b|@import|url\(|font-family|<text\b|href=", t), f"{p.name} has an external or font dependency"
        assert "#df8e1d" in t.lower(), f"{p.name} lost the sparkle colour"


def test_logo_configmap_ships_every_file_under_1mib(configmaps):
    cm = configmaps["forgejo-img"]
    assert set(cm_files(cm)) == set(IMG_FILES)
    assert cm_size(cm) < MIB


def test_logo_mounted_at_public_img():
    text = RELEASE.read_text()
    assert "name: forgejo-img" in text
    assert re.search(r"name: img\n\s+mountPath: /data/gitea/public/assets/img\n\s+readOnly: true", text)


def test_fixed_themes_swap_to_baked_logos():
    css = CSS.read_text()
    assert "#navbar-logo img{width:auto}" in css
    latte, mocha = hashed_logo("latte"), hashed_logo("mocha")
    for flavour, f in (("latte", latte), ("light", latte), ("mocha", mocha), ("frappe", mocha), ("macchiato", mocha), ("dark", mocha)):
        assert re.search(rf'\[data-theme\*="{flavour}"\][^{{]*\{{content:url\("\.\./img/{re.escape(f)}"\)\}}', css), flavour
    for f in (latte, mocha):
        assert (IMG / f).is_file()


# --- realliance/brand sync (scripts/sync-brand.sh, cluster/forgejo/custom/brand.lock.json) ---
ASSETS = CUSTOM / "public/assets"


def test_hashed_names_match_their_content():
    for d in ("css", "fonts", "img"):
        for p in (ASSETS / d).iterdir():
            m = HASHED.match(p.name)
            if m:
                assert hashlib.sha256(p.read_bytes()).hexdigest()[:8] == m["h"], f"{d}/{p.name}: name does not match its content"


def test_shipped_stylesheet_is_the_hashed_copy_of_the_source():
    assert (ASSETS / "css" / shipped_css_name()).read_bytes() == CSS.read_bytes(), (
        "realliance-fonts.css changed without a new hashed copy: run scripts/sync-brand.sh <current tag>")


def test_no_stale_hashed_copies(configmaps):
    shipped = set()
    for name in ("forgejo-font-css", "forgejo-fonts", "forgejo-img"):
        shipped |= set(cm_files(configmaps[name]))
    for d in ("css", "fonts", "img"):
        for p in (ASSETS / d).iterdir():
            if HASHED.match(p.name):
                assert p.name in shipped, f"{d}/{p.name} is hashed but no ConfigMap ships it (stale: re-run sync-brand.sh)"


def test_brand_lock_lists_what_ships():
    lock = json.loads(LOCK.read_text())
    assert re.fullmatch(r"v\d+\.\d+\.\d+", lock["tag"]) and re.fullmatch(r"[0-9a-f]{40}", lock["commit"])
    for rel, f in lock["files"].items():
        p = ASSETS / rel
        assert p.is_file(), f"brand.lock.json lists {rel}, which is missing"
        assert hashlib.sha256(p.read_bytes()).hexdigest() == f["sha256"], f"{rel} differs from what sync-brand.sh wrote for {lock['tag']}"
