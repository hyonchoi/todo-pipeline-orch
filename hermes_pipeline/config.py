from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
import tomllib

KanbanAdapterName = Literal["null", "hermes"]

@dataclass(frozen=True)
class Config:
    lock_dir: Path = field(default_factory=lambda: Path.home() / ".hermes" / "pipeline_locks")
    projects_dir: Path = field(default_factory=lambda: Path.home() / "projects")
    state_dir: Path = field(default_factory=lambda: Path.home() / ".hermes")
    claude_cmd: str = "claude"
    checkpoint_subdir: str = ".hermes/pipeline_checkpoints"
    ready_for_review_subdir: str = ".hermes/ready_for_review"
    counter_file_subpath: str = ".hermes/todo_id_counter"
    default_timeout: int = 1800
    kanban_adapter: KanbanAdapterName = "null"
    kanban_outbox_cap: int = 500
    log_file_subpath: str = "pipeline.log"
    log_retention_days: int = 7
    slack_channel: str = ""

    @classmethod
    def default(cls) -> "Config":
        return cls()

    @classmethod
    def from_env(cls) -> "Config":
        c = cls.default()
        env_map = {
            "PIPELINE_LOCK_DIR": ("lock_dir", Path),
            "PIPELINE_PROJECTS_DIR": ("projects_dir", Path),
            "PIPELINE_STATE_DIR": ("state_dir", Path),
            "PIPELINE_CLAUDE_CMD": ("claude_cmd", str),
            "PIPELINE_KANBAN_ADAPTER": ("kanban_adapter", str),
            "PIPELINE_SLACK_CHANNEL": ("slack_channel", str),
        }
        overrides = {}
        for env_key, (attr, ctor) in env_map.items():
            val = os.environ.get(env_key)
            if val is not None:
                overrides[attr] = ctor(val)
        if not overrides:
            return c
        from dataclasses import replace
        return replace(c, **overrides)

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
    backoff_interval_min: int = 30
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
    """
    import re

    if not slug or slug in (".", ".."):
        return False
    if slug.startswith(("-", ".")):
        return False
    if ".." in slug:
        return False
    return bool(re.match(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$', slug))


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
