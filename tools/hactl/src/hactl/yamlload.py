"""YAML loading with Home Assistant's custom tags.

HA config uses !include, !include_dir_*, !secret, !env_var and !input, which
plain yaml.safe_load rejects. `load` keeps them as Tagged placeholders, or
resolves !include when asked (dashboards pushed to the preview).
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from hactl.errors import HactlError


@dataclass(frozen=True)
class Tagged:
    tag: str
    value: Any


def _construct_any(loader, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


def load(path, *, resolve_includes: bool = False, allow_secret: bool = True) -> Any:
    path = Path(path)

    class Loader(yaml.SafeLoader):
        pass

    def include(loader, node):
        name = loader.construct_scalar(node)
        if not resolve_includes:
            return Tagged("!include", name)
        target = path.parent / name
        if not target.is_file():
            raise HactlError(f"{path.name}: !include {name}: no such file")
        return load(target, resolve_includes=True, allow_secret=allow_secret)

    def secret(loader, node):
        name = loader.construct_scalar(node)
        if not allow_secret:
            raise HactlError(f"{path.name}: !secret {name} is not allowed here")
        return Tagged("!secret", name)

    def other(loader, suffix, node):
        return Tagged("!" + suffix, _construct_any(loader, node))

    Loader.add_constructor("!include", include)
    Loader.add_constructor("!secret", secret)
    Loader.add_multi_constructor("!", other)
    try:
        with path.open() as f:
            return yaml.load(f, Loader=Loader)
    except yaml.YAMLError as e:
        raise HactlError(f"{path}: {e}") from None


def load_dir(directory) -> dict:
    """Every *.yaml in a directory, keyed by file stem (like !include_dir_named)."""
    return {f.stem: load(f) for f in sorted(Path(directory).glob("*.yaml"))}
