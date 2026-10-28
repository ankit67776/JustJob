"""Loads config.toml and .env."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def load_env(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE per line. Variables already in the environment win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif value.startswith("#"):
            value = ""
        else:
            value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()   # inline comment
        if key and value:
            os.environ.setdefault(key, value)


def load_config(path: Path | None = None) -> dict:
    load_env(ROOT / ".env")
    with open(path or ROOT / "config.toml", "rb") as f:
        cfg = tomllib.load(f)
    for section in ("profile", "filters", "ranking", "fetch", "sources", "ai", "digest", "notify"):
        cfg.setdefault(section, {})
    cfg["filters"].setdefault("title", {})
    cfg["filters"].setdefault("location", {})
    return cfg


def resume_text(cfg: dict, limit: int = 4000) -> str:
    name = cfg["profile"].get("resume_file")
    if not name:
        return ""
    path = ROOT / name
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()[:limit]
