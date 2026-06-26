# Plan: Pipeline Modularization + TODOS Manager 스킬

## 목표

1. `pipeline_watcher.py`를 **uv/pip 관리 Python 패키지**로 모듈화 (재설치/업그레이드 가능)
2. **TODOS.md 작성/관리 스킬** 생성 — gstack 형식 기반 + 핵심 결정 사항 사전 정의

---

## Part 1: Python 패키지 모듈화

### Phase 1: 패키지 구조 설계

**프로젝트 구조:**

```
hermes-pipeline/
├── pyproject.toml
├── README.md
├── src/hermes_pipeline/
│   ├── __init__.py              # 버전 정보
│   ├── config.py                # 설정 로딩 (YAML/환경변수)
│   ├── watcher.py               # TODOS.md 변경 감지 + 프로젝트 발견
│   ├── runner.py                # 파이프라인 실행 (Phase 순회)
│   ├── phases.py                # Phase 정의 (config 기반)
│   ├── state.py                 # 체크포인트 + 락 + 해시
│   └── slack.py                 # Slack 알림 (send_message 연동)
├── bin/
│   └── pipeline-watch           # CLI 엔트리포인트 (bash 스크립트)
└── configs/
    └── phases.yaml              # Phase별 설정 (명령어, 도구, 턴수)
```

### Phase 2: Phase 설정 외부화

**현재 문제:** Phase 정의가 Python 코드에 하드코딩되어 있음 (10개 Phase, 각 5개 필드)

**해결:** `phases.yaml`로 분리

```yaml
# configs/phases.yaml
phases:
  - name: "Phase 2: Autoplan"
    prompt: |
      gstack autoplan skill을 사용하세요.

      선택된 TODO는 {todo_id}입니다 — 이미 TODOS.md에서 in-flight로 표시됩니다.
      {todo_id}만 작업하세요.

      1. {todo_id}에 대한 CEO/Eng/UI/DX 리뷰를 수행하세요.
      2. 모든 계획 및 보조 문서를 docs/pipeline/에 저장하세요 (예: {todo_id}-plan.md).
      3. main에서 새 브랜치를 생성하세요.
      4. 생성된 문서를 커밋하세요.
      5. 브랜치명을 .hermes/pipeline_branch.txt에 저장하세요.
    tools: "Read,Write,Bash"
    turns: 20
    timeout: 1800

  - name: "Phase 3: Writing Plan"
    prompt: "writing-plans skill을 사용하세요. docs/pipeline/{todo_id}-plan.md를 읽어서 superpowers 형식으로 변환 후 docs/pipeline/{todo_id}-impl-plan.md에 저장하세요. 생성된 문서를 커밋하세요."
    tools: "Read,Write,Bash"
    turns: 15
    timeout: 1800

  # ... Phase 4~8 ...

  - name: "Phase 6.1: CSO Security Review"
    prompt: "gstack cso 스킬을 사용하세요. 현재 브랜치의 보안 리뷰를 수행하세요."
    tools: "Read,Write,Bash"
    turns: 20
    timeout: 1800

  # Phase 6.2~6.4 동일 패턴

  - name: "Phase 7: Document Release"
    prompt: |
      릴리즈 문서를 생성하세요.
      1. CHANGELOG.md 생성 또는 업데이트
      2. RELEASE_NOTES.md 생성 또는 업데이트
      3. README.md 프로젝트 구조 업데이트
      4. 변경사항 커밋
    tools: "Read,Write,Edit,Bash"
    turns: 15
    timeout: 1800

  - name: "Phase 8: Finish Branch"
    prompt: "superpowers finish-a-development-branch 스킬을 사용하세요."
    tools: "Read,Write,Bash"
    turns: 15
    timeout: 1800
```

**설정 클래스:**

```python
# src/hermes_pipeline/phases.py
from dataclasses import dataclass
from pathlib import Path
import yaml

@dataclass
class Phase:
    name: str
    prompt: str
    tools: str
    turns: int
    timeout: int = 1800

def load_phases(config_path: str | None = None) -> list[Phase]:
    """YAML 설정 파일에서 Phase 목록 로딩"""
    if config_path is None:
        config_path = Path(__file__).parent.parent / "configs" / "phases.yaml"
    with open(config_path) as f:
        data = yaml.safe_load(f)
    return [Phase(**p) for p in data["phases"]]
```

### Phase 3: 설정 모듈화

**현재 문제:** 경로, 명령어가 하드코딩되어 있음

**해결:** `config.py`로 분리 + 환경변수 오버라이드

```python
# src/hermes_pipeline/config.py
from pathlib import Path

class Config:
    LOCK_DIR = Path.home() / ".hermes" / "pipeline_locks"
    PROJECTS_DIR = Path.home() / "projects"
    CLAUDE_CMD = "claude"
    CHECKPOINT_DIR = ".hermes/pipeline_checkpoints"
    DEFAULT_TIMEOUT = 1800  # 30분

    @classmethod
    def override(cls, **kwargs):
        for key, value in kwargs.items():
            if hasattr(cls, key):
                setattr(cls, key, value)

    @classmethod
    def from_env(cls):
        """환경변수로 오버라이드"""
        env_map = {
            "PIPELINE_LOCK_DIR": "LOCK_DIR",
            "PIPELINE_PROJECTS_DIR": "PROJECTS_DIR",
            "PIPELINE_CLAUDE_CMD": "CLAUDE_CMD",
        }
        import os
        for env_key, attr_name in env_map.items():
            val = os.environ.get(env_key)
            if val:
                setattr(cls, attr_name, Path(val) if "DIR" in attr_name else val)
```

### Phase 4: 상태 관리 리팩토링

**현재 문제:** 상태 관리 함수가 전역 함수로 분리됨

**해결:** `State` 클래스로 캡슐화

```python
# src/hermes_pipeline/state.py
import json
import shutil
from pathlib import Path
from .config import Config

class State:
    def __init__(self, project: str):
        self.project = project
        self.lock_dir = Config.LOCK_DIR
        self.checkpoint_dir = Path(Config.PROJECTS_DIR, project, Config.CHECKPOINT_DIR)

    # 변경 감지
    def get_saved_hash(self) -> str | None: ...
    def save_hash(self, h: str): ...

    # 락
    def is_locked(self) -> bool: ...
    def lock(self): ...
    def unlock(self): ...

    # 체크포인트
    def last_completed_phase(self) -> int: ...
    def mark_phase_done(self, phase_key: str): ...

    # 초기화
    def reset(self): ...
```

### Phase 5: 파이프라인 러너 리팩토링

**`runner.py`:**

```python
# src/hermes_pipeline/runner.py
import subprocess
from .config import Config
from .phases import Phase, load_phases
from .state import State

class PipelineRunner:
    def __init__(self, project: str, project_dir: str, channel: str = ""):
        self.project = project
        self.project_dir = project_dir
        self.channel = channel
        self.state = State(project)
        self.phases = load_phases()

    def run_phase(self, phase: Phase) -> subprocess.CompletedProcess:
        """하나의 Phase를 Claude Code -p로 실행"""
        cmd = [
            Config.CLAUDE_CMD, "-p", phase.prompt,
            "--allowedTools", phase.tools,
            "--max-turns", str(phase.turns),
            "--output-format", "json",
        ]
        return subprocess.run(
            cmd, cwd=self.project_dir,
            capture_output=True, text=True,
            timeout=phase.timeout,
        )

    def run(self, from_phase: int = 0) -> bool:
        """전체 파이프라인 실행 (체크포인트 기반 재개)"""
        self.state.lock()
        if from_phase == 0:
            from_phase = self.state.last_completed_phase() + 1

        for idx, phase in enumerate(self.phases):
            if idx < from_phase:
                continue
            result = self.run_phase(phase)
            if result.returncode != 0:
                self.state.unlock()
                return False
            self.state.mark_phase_done(phase.phase_key)

        self.state.unlock()
        return True
```

### Phase 6: Slack 알림 모듈

**`slack.py`:**

```python
# src/hermes_pipeline/slack.py
import subprocess

def notify(channel: str, emoji: str, message: str):
    """Slack 채널에 알림 전송"""
    # hermes CLI 또는 직접 send_message 호출
    cmd = ["hermes", "chan", "message", channel, f"{emoji} {message}"]
    subprocess.run(cmd, capture_output=True)
```

### Phase 7: CLI 엔트리포인트

**`bin/pipeline-watch`:**

```bash
#!/bin/bash
# Cron Job용 - no_agent=True 스크립트
python3 -m hermes_pipeline.watcher "$@"
```

**주요 CLI 옵션:**
- `--auto` — 프로젝트 자동 발견 + 변경 감지 + 파이프라인 실행
- `--list` — 프로젝트 목록 표시
- `--project <name>` — 특정 프로젝트만 실행
- `--from-phase <N>` — Phase N부터 재개
- `--reset <name>` — 체크포인트 초기화
- `--config <path>` — Phase 설정 파일 경로

### Phase 8: Cron Job 등록

```python
cronjob(
    action="create",
    schedule="5m",
    no_agent=True,
    script="pipeline-watch --auto",
    name="Pipeline Watcher"
)
```

---

## Part 2: TODOS Manager 스킬

### 목적

TODOS.md에 새로운 작업 항목을 추가하는 스킬. gstack 형식을 따르되, **사용자 결정이 필요한 핵심 사항을 미리 정의**하여 Claude Code가 바로 실행 가능한 상태로 만든다.

### TODOS.md 형식 (gstack 기준)

각 항목의 필드:

| 필드 | 설명 | 필수 여부 |
|------|------|-----------|
| **ID** | `TODO-<n>` — 항목 헤딩에 포함, 발급 후 불변 | ✅ (자동 발급) |
| **What** | 작업 내용 | ✅ |
| **Why** | 이유/배경 | ✅ |
| **Pros** | 장점 | ✅ |
| **Cons** | 단점 | ✅ |
| **Context** | 구현 맥락/시작점 | ✅ |
| **Depends on** | 의존성 (`TODO-<n>` 참조, 같은 프로젝트 내) | 조건부 |

상태 표시:
- `[ ]` — 대기 중
- `[→]` — 처리 중
- `[x]` — 완료
- `[~]` — 유보

**ID 발급 규칙** (오케스트레이터 리팩터링 설계 — `docs/gstack/hyonchoi-main-design-20260610-195349.md`
Premise 5/Approach A 참조): `TODO-<n>`은 프로젝트별 단조 증가 카운터 파일
(`.hermes/todo_id_counter`)에서 한 번만 발급되며, 재사용/재번호 부여 금지. 항목이 완료/삭제
되어도 그 번호는 영구히 비워둔다 — 의존성, 브랜치 attempt 추적, kanban 카드 연결, 락/상태
파일에서 안전하게 참조 가능해야 하므로.

### 스킬 설계: `todos-manager` (가칭 "add-todo")

**동작 흐름:**

```
1. 프로젝트 디렉토리 확인 (TODOS.md 존재, 없으면 .hermes/todo_id_counter도 0으로 초기화)
2. 사용자 입력: 작업 설명 (자연어)
3. .hermes/todo_id_counter를 읽어 다음 TODO-<n> ID를 발급하고 카운터 증가
4. gstack 형식으로 TODOS 항목 작성:
   - What/Why/Pros/Cons/Context 도출
   - 핵심 결정 사항 사전 정의:
     a. 우선순위 (P1~P4)
     b. 노력 추정 (S/M/L/XL)
     c. Phase 매핑 (어떤 Phase에서 처리될 작업인가)
     d. Slug (브랜치명에 쓰일 짧은 식별자 — 버전 prefix는 오케스트레이터가 부여)
5. Depends on에 입력된 TODO-<n>이 실제 TODOS.md에 존재하는지, 그리고 추가 시 의존성
   사이클이 생기지 않는지 검사 — 사이클이면 경고하고 사용자에게 재확인
6. TODOS.md에 항목 추가 (헤딩: `## TODO-<n>: [작업명]`)
7. Slack 채널에 알림
```

이 스킬은 **kanban에 직접 쓰지 않는다** — kanban은 오케스트레이터가 "현재 진행 중인
작업"만 1프로젝트당 1카드로 관리하므로(Premise 8), 백로그 추가 시점에는 kanban 동기화가
필요 없다.

**핵심 결정 사항 사전 정의 템플릿:**

```markdown
## TODO-N: [작업명]

**What:** [작업 내용]

**Why:** [이유]

**Pros:**
- [장점1]
- [장점2]

**Cons:**
- [단점1]
- [단점2]

**Context:** [구현 시작점/맥락]

**Depends on:** [TODO-<n>, ... / 없으면 없음]

**Decisions:**
- Priority: P1 / P2 / P3 / P4
- Effort: S / M / L / XL
- Phase: 4 (Development) / 6.1 (CSO) / ...
- Slug: short-desc (오케스트레이터가 `feat/{base_version}-{slug}` 형식으로 브랜치 생성 시 사용)
- Test Coverage: 필요 / 불필요
- Security Review: 필요 / 불필요
```

`N`은 위 ID 발급 규칙에 따라 스킬이 자동으로 채운다 — 사용자가 직접 번호를 매기지 않는다.

### 스킬 프론트매터

```yaml
---
name: todos-manager
description: "TODOS.md 항목 추가 및 관리 — gstack 형식 기반, TODO-<n> ID 자동 발급, 핵심 결정 사항 사전 정의"
version: 1.0.0
author: hyonchoi
license: MIT
metadata:
  hermes:
    tags: [todos, gstack, planning, pipeline]
    related_skills: [gstack-plan-eng-review, gstack-office-hours]
---
```

---

## 실행 계획

### 1단계: 패키지 구조 생성

- [ ] `hermes-pipeline/` 디렉토리 생성
- [ ] `pyproject.toml` 작성
- [ ] `src/hermes_pipeline/` 모듈 구조 생성
- [ ] `configs/phases.yaml` 작성

### 2단계: 코어 모듈 구현

- [ ] `config.py` — 설정 클래스 + 환경변수 오버라이드
- [ ] `state.py` — State 클래스 (락/체크포인트/해시)
- [ ] `phases.py` — Phase 로딩 (YAML 기반)
- [ ] `runner.py` — PipelineRunner 클래스

### 3단계: 감시자 + 알림

- [ ] `watcher.py` — 변경 감지 + 프로젝트 발견
- [ ] `slack.py` — Slack 알림

### 4단계: CLI + Cron

- [ ] `bin/pipeline-watch` 엔트리포인트
- [ ] Cron Job 등록 (5분 폴링)

### 5단계: TODOS Manager 스킬

- [ ] `todos-manager` 스킬 생성 (SKILL.md)
- [ ] TODOS.md 템플릿 + 결정 사항 필드 정의
- [ ] 테스트 (test-project 기준)

### 6단계: 설치 + 검증

- [ ] `pip install -e ./hermes-pipeline` 로컬 설치
- [ ] Cron Job 실행 검증
- [ ] Slack 알림 검증

---

## 의사결정

1. **Phase 6 분리:** Phase 6.1~6.4는 별도의 Phase로 유지 (각각 별도 체크포인트)
2. **Phase 1 제외:** 현재 파이프라인은 Phase 2부터 시작 (office-hours는 Collaboration Mode에서 처리)
3. **Slack 알림:** `hermes chan message` CLI를 통해 전송 (send_message tool 직접 호출 불가)
4. **gstack-autoplan:** 별도 스킬이 아님 — Phase 2 prompt에서 스킬 참조 형태로 사용
5. **TODOS.md 형식:** gstack 기준 (What/Why/Pros/Cons/Context/Depends on) 유지 + Decisions 필드 추가

---

## Lane F: CLI, Watcher, Status, and Installation

**Status:** COMPLETE (TF.1-TF.6)

Implements the final lane bringing together all pieces into executable commands:

- **TF.1** `watcher.py`: Auto-tick discovery with per-project isolation. Discovers all projects with TODOS.md, detects changes via hash comparison, selects eligible TODOs, and isolates parse errors per project.
- **TF.2** `status.py`: Pending-records table printer. Collects ready_for_review records and formats them as a human-readable table with project, TODO ID, branch, PR URL, merge status, and age.
- **TF.3** `cli.py`: Argparse subcommands (`auto`, `merge`, `status`). Fully featured with config loading, logging setup, and per-command dispatch.
- **TF.4** `install-cron.sh`: Idempotent 5-minute cron registration helper.
- **TF.5** Documentation updates: README.md and this plan now document the full feature set and architecture.
- **TF.6** Full verification: All 192 tests pass; CLI help and subcommands verified.

See `docs/gstack/hermes-pipeline/design-plan.md` for architectural details.

---

## 2026-06-13 update

- Open Q1 — Hermes command repo path: **resolved.** `pipeline-tick` and
  `pipeline-phase` command defs live at `~/.hermes/commands/` (the user's
  local Hermes config repo). Cross-repo contract is the Python schema
  imports from `hermes_pipeline.decision`.
- Open Q3 — log routing: **resolved.** stdout-only. Hermes is the log sink;
  no local file logging from the pipeline package.

Both removed from "Open Questions".
