"""Repo locations hactl works with."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
HA_DIR = REPO / "cluster" / "home-assistant"
RELEASE_FILE = REPO / "cluster" / "applications" / "home-assistant-release.yaml"
TOKEN_FILE = REPO / "tools" / "hactl" / "agent.secret.yaml"


def rel(path) -> str:
    """Repo-relative path for messages."""
    try:
        return str(Path(path).resolve().relative_to(REPO))
    except ValueError:
        return str(path)
