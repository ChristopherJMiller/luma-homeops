"""Config lint: the rules that keep HA config deployable.

`lint --offline` needs no HA (CI runs it). Plain `lint` adds live checks.
"""
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from hactl import output, paths, refs, revision, yamlload
from hactl.errors import HactlError

_SLUG = re.compile(r"^[a-z0-9_]+\.yaml$")
_BOOLISH = re.compile(r"^\s*(?:-\s+)?[A-Za-z_][\w-]*:\s+(on|off|yes|no|On|Off|ON|OFF|Yes|No|YES|NO)\s*(?:#.*)?$")
_TIME = re.compile(r"^\s*(?:-\s+)?[A-Za-z_][\w-]*:\s+(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:#.*)?$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_CHECK_MARKERS = re.compile(
    r"ERROR|Invalid config|could not be validated|will not be initialized|invalid slug|Setup of package .* failed|Failed to",
    re.I,
)
_SECRET = re.compile(r"!secret\s+([A-Za-z0-9_]+)")
# Custom integrations the packages configure; check_config needs their code.
CUSTOM_COMPONENTS = {"adaptive_lighting": "https://github.com/basnijholt/adaptive-lighting"}


@dataclass
class Finding:
    rule: str
    path: str
    line: int | None
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else self.path
        return f"{self.severity.upper():7} {self.rule:16} {loc}  {self.message}"


def check_filenames(ha_dir: Path) -> list:
    return [
        Finding("filename", paths.rel(f), None, "package filenames must be lowercase with underscores (HA silently skips others)")
        for f in sorted((ha_dir / "packages").glob("*.yaml"))
        if not _SLUG.match(f.name)
    ]


def check_kustomization(ha_dir: Path) -> list:
    k = yaml.safe_load((ha_dir / "kustomization.yaml").read_text()) or {}
    listed = {f for g in k.get("configMapGenerator", []) for f in g.get("files", [])}
    out = []
    for sub in revision.SOURCES:
        for f in sorted((ha_dir / sub).glob("*.yaml")):
            if f"{sub}/{f.name}" not in listed:
                out.append(Finding("kustomization", paths.rel(f), None,
                                   "not listed in kustomization.yaml configMapGenerator; it will never reach HA"))
    for relf in sorted(listed):
        if not (ha_dir / relf).exists():
            out.append(Finding("kustomization", paths.rel(ha_dir / "kustomization.yaml"), None, f"lists {relf}, which does not exist"))
    return out


def check_quoting(ha_dir: Path) -> list:
    out = []
    for sub in revision.SOURCES:
        for f in sorted((ha_dir / sub).glob("*.yaml")):
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if m := _BOOLISH.match(line):
                    out.append(Finding("quoting", paths.rel(f), n, f"unquoted {m.group(1)} becomes a boolean in YAML 1.1; write '{m.group(1)}'"))
                elif m := _TIME.match(line):
                    out.append(Finding("quoting", paths.rel(f), n, f'unquoted {m.group(1)} becomes a number in YAML 1.1; write "{m.group(1)}"'))
    return out


def check_revision(ha_dir: Path) -> list:
    if revision.is_current(ha_dir):
        return []
    return [Finding("revision", paths.rel(ha_dir / revision.REVISION_FILE), None, "stale; run `hactl revision --write`")]


def deployed_tag(release_file: Path = paths.RELEASE_FILE) -> str:
    m = re.search(r"tag:\s*(\d{4}\.\d+\.\d+)", release_file.read_text())
    if not m:
        raise HactlError(f"no HA image tag found in {paths.rel(release_file)}")
    return m.group(1)


def _docker_ok() -> bool:
    return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def check_config_command(cfg: Path, tag: str) -> list:
    """docker run for check_config. Afterwards, hand every file back to whoever
    owns /config *inside* the container: that is the host user under rootless
    docker and the runner under rootful docker (CI), so cleanup works for both."""
    return ["docker", "run", "--rm", "-v", f"{cfg}:/config", "--entrypoint", "sh",
            f"docker.io/homeassistant/home-assistant:{tag}", "-c",
            'python -m homeassistant --script check_config -c /config; chown -R "$(stat -c %u:%g /config)" /config']


def check_config(ha_dir: Path, tag: str) -> list:
    """HA's own check_config in the deployed image. Its exit code is meaningless; grep it."""
    if not _docker_ok():
        return [Finding("check_config", "", None, "docker unavailable: check_config was NOT run", "warning")]
    with tempfile.TemporaryDirectory(prefix="hactl-check-") as tmp:
        cfg = Path(tmp) / "config"
        shutil.copytree(ha_dir / "packages", cfg / "packages")
        shutil.copytree(ha_dir / "dashboards", cfg / "dashboards")
        if (ha_dir / "blueprints").exists():
            shutil.copytree(ha_dir / "blueprints", cfg / "blueprints")
        (cfg / "configuration.yaml").write_text("homeassistant:\n  packages: !include_dir_named packages/\n")
        names = sorted({n for f in (cfg / "packages").glob("*.yaml") for n in _SECRET.findall(f.read_text())})
        (cfg / "secrets.yaml").write_text("".join(f'{n}: "lint-dummy"\n' for n in names))
        components = cfg / "custom_components"
        components.mkdir()
        for name, repo in CUSTOM_COMPONENTS.items():
            src = Path(tmp) / f"{name}-src"
            subprocess.run(["git", "clone", "-q", "--depth", "1", repo, str(src)], check=True)
            shutil.copytree(src / "custom_components" / name, components / name)
        r = subprocess.run(check_config_command(cfg, tag), capture_output=True, text=True)
        text = _ANSI.sub("", r.stdout + r.stderr)
        return [Finding("check_config", f"(HA {tag})", None, line.strip()) for line in text.splitlines() if _CHECK_MARKERS.search(line)]


def offline(ha_dir: Path = paths.HA_DIR, run_check_config: bool = True) -> list:
    findings = check_filenames(ha_dir) + check_kustomization(ha_dir) + check_quoting(ha_dir) + check_revision(ha_dir)
    if run_check_config:
        findings += check_config(ha_dir, deployed_tag())
    return findings


def _line_of(path: Path, needle: str):
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if needle in line:
            return n
    return None


ALLOW_MISSING = paths.HA_DIR / "lint-allow-missing.txt"


def load_allow_missing(path: Path = ALLOW_MISSING) -> set:
    """Entity ids that are referenced on purpose before they exist (one per line, # comments)."""
    if not path.exists():
        return set()
    return {line.split("#")[0].strip() for line in path.read_text().splitlines()} - {""}


def check_entity_refs(ha_dir: Path, known: set, allow: set = frozenset()) -> list:
    declared = refs.declared_entities(yamlload.load_dir(ha_dir / "packages"))
    out = []
    for sub in ("packages", "dashboards"):
        for f in sorted((ha_dir / sub).glob("*.yaml")):
            for ref in sorted(refs.extract_refs(yamlload.load(f))):
                if ref not in known and ref not in declared and ref not in allow:
                    out.append(Finding("entity-ref", paths.rel(f), _line_of(f, ref), f"{ref} does not exist in HA"))
    return out


def template_strings(obj) -> list:
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in template_strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in template_strings(v)]
    if isinstance(obj, str) and ("{{" in obj or "{%" in obj):
        return [obj]
    return []


def check_dashboard_templates(ha_dir: Path, render) -> list:
    out = []
    for f in sorted((ha_dir / "dashboards").glob("*.yaml")):
        for text in template_strings(yamlload.load(f)):
            try:
                render(text)
            except HactlError as e:
                first = text.strip().splitlines()[0][:60]
                out.append(Finding("dashboard-template", paths.rel(f), _line_of(f, first), str(e)[:300]))
    return out


def live(ha_dir: Path, client) -> list:
    states = client.get("/api/states")
    entities = client.ws({"type": "config/entity_registry/list"})[0]
    known = {s["entity_id"] for s in states} | {e["entity_id"] for e in entities}
    # Card templates may use the card's own variables; give them harmless values.
    variables = {"entity": "", "user": "", "config": {}}

    def render(text):
        client.post("/api/template", {"template": text, "variables": variables}, raw=True)

    allow = load_allow_missing(ha_dir / "lint-allow-missing.txt")
    return check_entity_refs(ha_dir, known, allow) + check_dashboard_templates(ha_dir, render)


def _run(args) -> int:
    findings = offline(paths.HA_DIR, run_check_config=not args.no_check_config)
    if not args.offline:
        from hactl.client import Client

        findings += live(paths.HA_DIR, Client())
    output.emit(args, [asdict(f) for f in findings], [str(f) for f in findings] or ["lint: clean"])
    return 1 if any(f.severity == "error" for f in findings) else 0


def register(sub) -> None:
    p = sub.add_parser("lint", parents=[output.COMMON], help="check HA config (CI runs --offline)")
    p.add_argument("--offline", action="store_true", help="only the checks that need no HA (what CI runs)")
    p.add_argument("--no-check-config", action="store_true", help="skip the docker check_config run")
    p.set_defaults(func=_run)
