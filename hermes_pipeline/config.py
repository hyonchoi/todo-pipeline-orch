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
    model: str = "claude-opus-4-7"
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
