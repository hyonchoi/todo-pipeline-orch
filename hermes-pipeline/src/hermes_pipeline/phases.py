from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import yaml

@dataclass(frozen=True)
class Phase:
    phase_key: str
    name: str
    prompt: str
    tools: str
    turns: int
    timeout: int = 1800

def load_phases(config_path: Path | str | None = None) -> list[Phase]:
    if config_path is None:
        config_path = Path(__file__).resolve().parent.parent.parent / "configs" / "phases.yaml"
    config_path = Path(config_path)
    with open(config_path) as f:
        data = yaml.safe_load(f)
    return [Phase(**p) for p in data["phases"]]
