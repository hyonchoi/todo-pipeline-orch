from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

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
