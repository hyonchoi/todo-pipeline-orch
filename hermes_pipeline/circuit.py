"""Circuit breaker — N consecutive no-progress ticks -> backoff + Slack alert."""
from __future__ import annotations
import datetime as _dt
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)

def _send_slack(*, channel: str, msg: str) -> None:
    try:
        subprocess.run(["hermes", "chan", "message", channel, msg], timeout=10, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

def _set_cron_interval(*, minutes: int) -> None:
    try:
        subprocess.run(
            ["hermes", "cron", "set", "pipeline-tick", f"*/{minutes} * * * *"],
            timeout=10, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

@dataclass
class CircuitBreaker:
    state_path: Path
    no_progress_threshold: int
    backoff_interval_min: int
    alert_dedup_hours: int
    slack_channel: str

    def _load(self) -> dict:
        if not self.state_path.exists():
            return {"consecutive_no_progress": 0, "last_alert_at": None, "backed_off": False}
        return json.loads(self.state_path.read_text())

    def _save(self, st: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(st, sort_keys=True))

    def observe(self, *, picked: str | None, counts_as_no_progress: bool) -> None:
        st = self._load()
        if picked is not None:
            st["consecutive_no_progress"] = 0
            if st.get("backed_off"):
                _set_cron_interval(minutes=5)
                st["backed_off"] = False
            self._save(st)
            return
        if not counts_as_no_progress:
            self._save(st)
            return
        st["consecutive_no_progress"] += 1
        if st["consecutive_no_progress"] >= self.no_progress_threshold and not st.get("backed_off"):
            last = st.get("last_alert_at")
            dedup_ok = True
            if last:
                last_dt = _dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
                if (_now() - last_dt).total_seconds() < self.alert_dedup_hours * 3600:
                    dedup_ok = False
            if dedup_ok:
                _send_slack(
                    channel=self.slack_channel,
                    msg=f"pipeline-tick: {st['consecutive_no_progress']} consecutive no-progress ticks; backing off to {self.backoff_interval_min}m",
                )
                st["last_alert_at"] = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
            _set_cron_interval(minutes=self.backoff_interval_min)
            st["backed_off"] = True
        self._save(st)
