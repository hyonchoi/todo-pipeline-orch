#!/usr/bin/env python3
"""
Pipeline Watcher: TODOS.md 변경 감지 → Claude Code 파이프라인 자동 실행

프로젝트 발견: Slack project__* private 채널 자동 탐지
상태 관리: hash 기반 변경 감지 + lock 파일 기반 중복 실행 방지
체크포인트: Phase 완료 시 마커 파일로 재시작 시점 기억

사용법:
  python3 ~/.hermes/scripts/pipeline_watcher.py --project test-project
  python3 ~/.hermes/scripts/pipeline_watcher.py --auto-discover
  python3 ~/.hermes/scripts/pipeline_watcher.py --reset test-project   # 체크포인트 초기화
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ── 설정 ──────────────────────────────────────────────────────────────
LOCK_DIR = Path.home() / ".hermes" / "pipeline_locks"
PROJECTS_DIR = Path.home() / "projects"
CLAUDE_CMD = "claude"  # Claude Code CLI

# Phase별 설정 (명령어, 허용 도구, 최대 턴수)
PHASES = [
    {
        "name": "Phase 2: Autoplan",
        "prompt": """gstack autoplan 스킬을 사용하세요.

1. TODOS.md를 읽고 다음 작업을 선택하세요
2. CEO/ENG/UI/DX 리뷰를 수행하세요  
3. Plan 문서를 .hermes/plans/에 생성하세요
4. main에서 새 브랜치를 생성하고 계획서를 커밋하세요
5. 브랜치명을 .hermes/pipeline_branch.txt에 저장하세요""",
        "tools": "Read,Write,Bash",
        "turns": 20,
    },
    {
        "name": "Phase 3: Writing Plan",
        "prompt": """superpowers writing-plan 스킬을 사용하세요.

.hermes/plans/의 plan 문서를 superpowers 형식으로 변환하세요.""",
        "tools": "Read,Write,Bash",
        "turns": 15,
    },
    {
        "name": "Phase 4: Development",
        "prompt": """superpowers subagent-driven-development 스킬을 사용하세요.

plan 문서를 따라 코드를 구현하세요.""",
        "tools": "Read,Write,Edit,Bash",
        "turns": 50,
    },
    {
        "name": "Phase 5: Review",
        "prompt": """gstack review 스킬을 사용하세요.

구현된 결과를 리뷰하세요.""",
        "tools": "Read,Bash",
        "turns": 20,
    },
    {
        "name": "Phase 6.1: CSO Security Review",
        "prompt": """gstack cso 스킬을 사용하세요.

현재 브랜치의 보안 리뷰를 수행하고, 보안 리포트 생성 시 Write 권한이 필요하다면
.hermes/security-report.json에 결과를 저장하세요.""",
        "tools": "Read,Write,Bash",
        "turns": 20,
    },
    {
        "name": "Phase 6.2: QA Test",
        "prompt": """gstack qa 스킬을 사용하세요.

현재 브랜치의 QA 테스트를 수행하세요.
테스트 결과가 있다면 .hermes/qa-report.json에 저장하세요.""",
        "tools": "Read,Write,Bash",
        "turns": 15,
    },
    {
        "name": "Phase 6.3: Design/UI Review",
        "prompt": """gstack design-review 스킬을 사용하세요.

현재 브랜치의 UI/디자인 리뷰를 수행하세요.
리뷰 결과가 있다면 .hermes/design-review-report.json에 저장하세요.""",
        "tools": "Read,Write,Bash",
        "turns": 15,
    },
    {
        "name": "Phase 6.4: DX Review",
        "prompt": """gstack devex-review 스킬을 사용하세요.

현재 브랜치의 개발자 경험 리뷰를 수행하세요.
리뷰 결과가 있다면 .hermes/dx-review-report.json에 저장하세요.""",
        "tools": "Read,Write,Bash",
        "turns": 15,
    },
    {
        "name": "Phase 7: Document Release",
        "prompt": """릴리즈 문서를 생성하세요.

1. CHANGELOG.md를 생성 또는 업데이트하세요 (현재 브랜치의 변경사항 포함)
2. RELEASE_NOTES.md를 생성 또는 업데이트하세요 (v0.1.0 또는 다음 버전)
3. README.md의 프로젝트 구조를 업데이트하세요 (새 파일 추가 반영)
4. 변경사항을 커밋하세요: "docs: add release documents for v0.1.0"

gstack document-release 스킬은 AskUserQuestion을 필요로 하므로 직접 수행하세요.""",
        "tools": "Read,Write,Edit,Bash",
        "turns": 15,
    },
    {
        "name": "Phase 8: Finish Branch",
        "prompt": """superpowers finish-a-development-branch 스킬을 사용하세요.

브랜치를 main에 머지하고 마무리하세요.""",
        "tools": "Read,Write,Bash",
        "turns": 15,
    },
]


# ── 유틸리티 함수 ────────────────────────────────────────────────────
def log(msg: str, flush: bool = True):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=flush)


def file_hash(path: str) -> str | None:
    """파일 MD5 해시 반환 (변경 감지용)"""
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except FileNotFoundError:
        return None


def is_locked(project: str) -> bool:
    lock_file = LOCK_DIR / f"{project}.lock"
    return lock_file.exists()


def create_lock(project: str):
    lock_file = LOCK_DIR / f"{project}.lock"
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "w") as f:
        json.dump(
            {"pid": os.getpid(), "start_time": time.time(), "project": project}, f
        )
    log(f"🔒 {project}: Lock 생성")


def remove_lock(project: str):
    lock_file = LOCK_DIR / f"{project}.lock"
    if lock_file.exists():
        lock_file.unlink()
        log(f"🔓 {project}: Lock 해제")


def get_saved_hash(project: str) -> str | None:
    state_file = LOCK_DIR / f"{project}.state.json"
    if state_file.exists():
        with open(state_file) as f:
            return json.load(f).get("todos_hash")
    return None


def save_hash(project: str, new_hash: str):
    state_file = LOCK_DIR / f"{project}.state.json"
    with open(state_file, "w") as f:
        json.dump({"todos_hash": new_hash, "updated": time.time()}, f)


def reset_project(project: str):
    """프로젝트의 체크포인트와 상태 초기화"""
    lock_file = LOCK_DIR / f"{project}.lock"
    state_file = LOCK_DIR / f"{project}.state.json"
    for f in [lock_file, state_file]:
        if f.exists():
            f.unlink()
            log(f"🗑  {project}: {f.name} 삭제")

    # 체크포인트 디렉토리 삭제
    checkpoints = Path.home() / "projects" / project / ".hermes" / "pipeline_checkpoints"
    if checkpoints.exists():
        import shutil
        shutil.rmtree(checkpoints)
        log(f"🗑  {project}: 체크포인트 전체 삭제")


def get_last_completed_phase(project: str) -> int:
    """체크포인트 기반 마지막 완료 Phase 인덱스 (0-based)"""
    checkpoints = Path.home() / "projects" / project / ".hermes" / "pipeline_checkpoints"
    if not checkpoints.exists():
        return -1

    last_idx = -1
    for idx in range(len(PHASES)):
        phase_name = PHASES[idx]["name"]
        match = re.search(r"Phase (\d+\.?\d*)", phase_name)
        if match:
            phase_key = f"phase_{match.group(1)}"
            if (checkpoints / f"{phase_key}_done").exists():
                last_idx = idx

    return last_idx


def mark_phase_done(project: str, phase_key: str):
    """Phase 완료 마커 생성 (phase_key 예: "2", "3", "6.1", "6.2", ..."""
    checkpoints = Path.home() / "projects" / project / ".hermes" / "pipeline_checkpoints"
    checkpoints.mkdir(parents=True, exist_ok=True)
    (checkpoints / f"phase_{phase_key}_done").touch()
    log(f"  ✅ Phase {phase_key} 체크포인트 기록")


def save_phase_state(project: str, phase_num: int, state: dict):
    """Phase별 상태 저장"""
    checkpoints = Path.home() / "projects" / project / ".hermes" / "pipeline_checkpoints"
    checkpoints.mkdir(parents=True, exist_ok=True)
    with open(checkpoints / f"phase_{phase_num}_state.json", "w") as f:
        json.dump(state, f, indent=2)


# ── Claude Code 실행 ────────────────────────────────────────────────
def run_claude_phase(
    project_dir: str,
    phase: dict,
    verbose: bool = True,
) -> subprocess.CompletedProcess:
    """Claude Code -p 모드로 한 Phase 실행"""
    if verbose:
        log(f"  🚀 {phase['name']} (max-turns={phase['turns']})")

    cmd = [
        CLAUDE_CMD,
        "-p",
        phase["prompt"],
        "--allowedTools",
        phase["tools"],
        "--max-turns",
        str(phase["turns"]),
        "--output-format",
        "json",
    ]

    if verbose:
        log(f"  $ {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=60 * 30,  # 30분 타임아웃
        )
        return result
    except subprocess.TimeoutExpired:
        log(f"  ⏰ {phase['name']} 타임아웃 (30분 초과)")
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="TIMEOUT"
        )


# ── 파이프라인 실행 ──────────────────────────────────────────────────
def run_pipeline(project: str, project_dir: str, channel: str = "", from_phase: int = 0) -> bool:
    """전체 파이프라인 실행 (체크포인트 기반 재개 지원)"""
    project_path = Path(project_dir)

    if from_phase == 0:
        last_idx = get_last_completed_phase(project)
        from_phase = last_idx + 1  # 완료된 인덱스의 다음 Phase부터
        if last_idx >= 0:
            phase_name = PHASES[last_idx]["name"]
            log(f"  🔄 {project}: {phase_name} 완료됨 → Phase {from_phase}부터 재개")
    else:
        from_phase = from_phase - 2  # from_phase는 Phase 번호 (예: 3) → 인덱스 (0)

    create_lock(project)
    log_file = LOCK_DIR / f"{project}.log"

    for idx, phase in enumerate(PHASES):
        if idx < from_phase:
            log(f"  ⏭  {phase['name']} (이미 완료 → 스킵)")
            continue

        # Phase 키 추출 (예: "Phase 6.1" → "6.1")
        match = re.search(r"Phase (\d+\.?\d*)", phase["name"])
        phase_key = match.group(1) if match else str(idx)

        log(f"▶ {phase['name']} 시작")

        # 로그 기록
        with open(log_file, "a") as f:
            f.write(f"\n--- {phase['name']} (시작) ---\n")

        result = run_claude_phase(project_dir, phase)

        # 결과 기록
        with open(log_file, "a") as f:
            f.write(f"  exit_code: {result.returncode}\n")
            if result.stdout:
                f.write(f"  stdout (first 1000 chars): {result.stdout[:1000]}\n")
            if result.stderr:
                f.write(f"  stderr: {result.stderr[:1000]}\n")
            f.write(f"--- {phase['name']} (종료) ---\n")

        if result.returncode != 0:
            # 실패 → JSON 결과 확인
            output_str = result.stdout.strip()
            try:
                result_json = json.loads(output_str)
                subtype = result_json.get("subtype", "unknown")
                if subtype == "error_max_turns":
                    log(f"  ❌ {phase['name']} 실패: 턴 수 초과 (max={phase['turns']})")
                elif subtype == "error_budget":
                    log(f"  ❌ {phase['name']} 실패: 예산 초과")
                else:
                    log(f"  ❌ {phase['name']} 실패: subtype={subtype}")
                    if "result" in result_json:
                        log(f"    메시지: {result_json['result'][:200]}")
            except (json.JSONDecodeError, KeyError):
                log(f"  ❌ {phase['name']} 실패 (exit_code={result.returncode})")
                if result.stderr:
                    log(f"    stderr: {result.stderr[:200]}")

            # 실패 시 Slack 알림 (나중에 연동)
            # save_phase_state(project, phase_num, {"status": "failed"})
            remove_lock(project)
            return False

        # 성공 시 체크포인트 기록
        mark_phase_done(project, phase_key)

        # 실패 여부와 무관하게 결과 파싱
        try:
            result_json = json.loads(result.stdout.strip())
            if result_json.get("subtype") == "success":
                cost = result_json.get("total_cost_usd", 0)
                turns = result_json.get("num_turns", 0)
                log(f"  ✅ {phase['name']} 완료 (turns={turns}, cost=${cost:.4f})")
            else:
                log(f"  ⚠️  {phase['name']} subtype={result_json.get('subtype')}")
        except (json.JSONDecodeError, KeyError):
            log(f"  ✅ {phase['name']} 완료")

    # 전체 완료
    remove_lock(project)
    log(f"🎉 {project}: 파이프라인 전체 완료!")
    return True


# ── 프로젝트 발견 ───────────────────────────────────────────────────
def discover_projects_auto() -> list[dict]:
    """Slack project__* 채널에서 프로젝트 자동 발견"""
    # Hermes send_message(action='list') 결과 파싱용
    # 직접 실행 시에는 디렉토리 기반 폴백 사용
    return []  # 크론에서 Hermes agent가 수행


def discover_projects_dir() -> list[dict]:
    """~/projects/ 디렉토리에서 프로젝트 발견"""
    projects = []
    if not PROJECTS_DIR.exists():
        return projects
    for entry in sorted(PROJECTS_DIR.iterdir()):
        if entry.is_dir() and (entry / "TODOS.md").exists():
            project_name = entry.name
            projects.append(
                {
                    "name": project_name,
                    "directory": str(entry),
                    "channel": f"project__{project_name}",
                }
            )
    return projects


# ── 메인 ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Pipeline Watcher")
    parser.add_argument("--project", type=str, help="특정 프로젝트만 실행")
    parser.add_argument("--auto-discover", action="store_true", help="프로젝트 자동 발견")
    parser.add_argument("--reset", type=str, help="프로젝트 체크포인트 초기화")
    parser.add_argument("--from-phase", type=int, default=0, help="특정 Phase부터 실행")
    parser.add_argument("--list", action="store_true", help="감지된 프로젝트 목록 표시")
    args = parser.parse_args()

    # 초기화
    LOCK_DIR.mkdir(parents=True, exist_ok=True)

    # 체크포인트 초기화
    if args.reset:
        reset_project(args.reset)
        return

    # 프로젝트 목록
    if args.project:
        projects = [
            {
                "name": args.project,
                "directory": str(PROJECTS_DIR / args.project),
                "channel": f"project__{args.project}",
            }
        ]
    else:
        projects = discover_projects_dir()

    # 목록만 표시
    if args.list:
        if not projects:
            print("📭 TODOS.md가 있는 프로젝트 없음")
            return
        print("📋 감지된 프로젝트:")
        for p in projects:
            todos_path = os.path.join(p["directory"], "TODOS.md")
            h = file_hash(todos_path)
            saved = get_saved_hash(p["name"])
            locked = is_locked(p["name"])
            last_phase = get_last_completed_phase(p["name"])
            status = []
            if h != saved:
                status.append("🔄 변경감지")
            else:
                status.append("✅ 동일")
            if locked:
                status.append("🔒 Lock")
            if last_phase >= 0:
                phase_name = PHASES[last_phase]["name"]
                status.append(f"{phase_name} 완료")
            print(f"  {p['name']:30s} {', '.join(status)}")
        return

    if not projects:
        log("📭 TODOS.md가 있는 프로젝트 없음")
        return

    log(f"📋 {len(projects)}개 프로젝트 감지")

    # 각 프로젝트 처리
    for proj in projects:
        name = proj["name"]
        project_dir = proj["directory"]
        channel = proj["channel"]
        todos_path = os.path.join(project_dir, "TODOS.md")

        log(f"\n{'=' * 60}")
        log(f"프로젝트: {name}")
        log(f"  디렉토리: {project_dir}")
        log(f"  채널: {channel}")

        # TODOS.md 존재 확인
        current_hash = file_hash(todos_path)
        if current_hash is None:
            log(f"  ⚠️  TODOS.md 없음 → 스킵")
            continue

        saved_hash = get_saved_hash(name)
        if current_hash == saved_hash:
            log(f"  ✅ TODOS.md 변경 없음 → 스킵")
            continue

        log(f"  🔄 TODOS.md 변경 감지 (hash: {current_hash[:8]}...)")

        # 중복 실행 방지
        if is_locked(name):
            log(f"  🔒 {name}: 이미 실행 중 (Lock 있음) → 스킵")
            continue

        # 파이프라인 실행
        success = run_pipeline(name, project_dir, channel, from_phase=args.from_phase)

        if success:
            save_hash(name, current_hash)
            log(f"  💾 {name}: 해시값 저장 (다음 폴링 대비)")


if __name__ == "__main__":
    main()
