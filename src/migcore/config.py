"""Engagement config loader with ${VAR} and ${VAR:-default} expansion (secrets stay in env)."""
from __future__ import annotations
import os, re
from pathlib import Path
import yaml

_PAT = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value):
    if isinstance(value, str):
        return _PAT.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def load_config(path: str | None = None) -> dict:
    path = path or os.environ.get("ENGAGEMENT_CONFIG", "config/engagement.yaml")
    raw = yaml.safe_load(Path(path).read_text())
    return _expand(raw)


def runs_dir() -> Path:
    p = Path(os.environ.get("RUNS_DIR", "./runs"))
    p.mkdir(parents=True, exist_ok=True)
    return p
