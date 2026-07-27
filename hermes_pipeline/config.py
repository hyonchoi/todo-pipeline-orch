from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    projects_dir: Path = field(default_factory=lambda: Path.home() / "projects")
    state_dir: Path = field(default_factory=lambda: Path.home() / ".hermes")
    log_file_subpath: str = "pipeline.log"
    log_retention_days: int = 7
    slack_channel: str = ""

    @classmethod
    def default(cls) -> Config:
        return cls()

    @classmethod
    def from_env(cls) -> Config:
        from .config_loader import load_global_config

        return load_global_config()

@dataclass(frozen=True)
class SelectionConfig:
    model: str = "auto"
    max_tokens: int = 4000
    auto_execute: bool = False
    prompt_path: str = ".hermes/prompts/selection.md"
    expected_prompt_sha: str | None = None

@dataclass(frozen=True)
class CircuitBreakerConfig:
    no_progress_threshold: int = 3
    alert_dedup_hours: int = 24
    max_phase_timeout_min: int = 120
    max_tick_duration_min: int = 10

@dataclass(frozen=True)
class FullConfig:
    base: Config
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)

    def __getattr__(self, name):
        return getattr(self.base, name)

def _validate_project_slug(slug: str) -> bool:
    """Reject project slugs that could inject CLI flags or traverse paths.

    Rules:
    - Must start with a letter or digit (no leading dash, dot, or underscore)
    - Only alphanumeric, single dash, single underscore, single dot (no consecutive
      dots that could form '..' path traversal)
    - No consecutive dots (blocks '..' path traversal)
    - No leading dash (blocks CLI flag injection)
    - Not a bare '.' or '..'
    - Minimum 2 characters (single-char slugs are too generic)
    """
    if not slug or slug in (".", ".."):
        return False
    if slug.startswith(("-", ".")):
        return False
    if ".." in slug:
        return False
    # \Z (not $) anchors the absolute end of string: $ also matches just
    # before a trailing newline, so "slug\n" would otherwise validate and the
    # newline could smuggle into a path or CLI argument.
    return bool(re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._-]+\Z', slug))


def _coerce_section(cls, data: dict):
    fields = {f.name for f in cls.__dataclass_fields__.values()}
    return cls(**{k: v for k, v in data.items() if k in fields})

def load_toml_overlay(base: Config, path: Path) -> FullConfig:
    p = Path(path)
    try:
        data = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"malformed TOML at {p}: {e}") from e
    sel = _coerce_section(SelectionConfig, data.get("selection", {}))
    cb = _coerce_section(CircuitBreakerConfig, data.get("circuit_breaker", {}))
    return FullConfig(base=base, selection=sel, circuit_breaker=cb)
