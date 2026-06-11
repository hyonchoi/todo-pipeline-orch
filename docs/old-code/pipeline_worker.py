#!/usr/bin/env python3
"""
Pipeline Worker — Phase 2~8 순차 실행 + 체크포인트 + 슬랙 알림
Kanban 태스크가 생성되면 Dispatcher가 이 스크립트를 실행

Usage:
  python3 pipeline_worker.py --project <name> --channel <slack_channel> --directory <path> --todo_hash <hash>
"""

import argparse
import json
import os
import subprocess
import sys
import time

CKPT_DIR = ".hermes/pipeline_checkpoints"
LOG_FILE = ".hermes/pipeline.log"
BRANCH_FILE = ".hermes/pipeline_branch.txt"

def run_phase(phase_num, phase_name, prompt, tools="Read,Write,Bash", max_turns=15):
    """Phase를 Claude Code -p 모드로 실행"""
    cmd = [
        "claude", "-p", prompt,
        "--allowedTools", tools,
        "--max-turns", str(max_turns),
        "--output-format", "json",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True, text=True,
        timeout=30 * 60,  # 30분 타임아웃
    )

    # 로그 기록
    with open(LOG_FILE, "a") as f:
        f.write(f"\n--- Phase {phase_num}: {phase_name} (exit={result.returncode}) ---\n")
        if result.stdout:
            f.write(f"stdout: {result.stdout[:3000]}\n")
        if result.stderr:
            f.write(f"stderr: {result.stderr[:3000]}\n")

    return result

def send_slack(channel, message):
    """슬랙 채널에 알림 전송"""
    # hermes CLI 사용 (no_agent 모드)
    result = subprocess.run(
        ["hermes", "chan", "message", channel, message],
        capture_output=True, text=True, timeout=30
    )
    return result.returncode == 0

def get_saved_hash(project):
    """저장된 해시값 읽기"""
    hash_dir = os.path.expanduser("~/.hermes/pipeline_hashes")
    hash_file = os.path.join(hash_dir, f"{project}.json")
    if os.path.exists(hash_file):
        with open(hash_file) as f:
            return json.load(f).get("hash")
    return None

def save_hash(project, h):
    """새 해시값 저장"""
    hash_dir = os.path.expanduser("~/.hermes/pipeline_hashes")
    os.makedirs(hash_dir, exist_ok=True)
    hash_file = os.path.join(hash_dir, f"{project}.json")
    with open(hash_file, "w") as f:
        json.dump({"hash": h}, f)

def main():
    parser = argparse.ArgumentParser(description="Pipeline Worker")
    parser.add_argument("--project", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--todo_hash", required=True)
    args = parser.parse_args()

    project = args.project
    channel = args.channel
    project_dir = os.path.expanduser(args.directory) if args.directory.startswith("~") else args.directory
    todo_hash = args.todo_hash

    os.chdir(project_dir)
    os.makedirs(CKPT_DIR, exist_ok=True)

    # 이전 로그 정리
    with open(LOG_FILE, "a") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"Pipeline started for {project} at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
        f.write(f"{'='*60}\n")

    # Phase 정의
    phases = [
        {
            "num": 2,
            "name": "Autoplan",
            "prompt": """gstack autoplan 스킬을 사용하세요:
1. TODOS.md를 읽고 가장 먼저 처리할 작업을 선택하세요
2. CEO 리뷰 (plan-ceo-review 스킬)
3. ENG 리뷰 (plan-eng-review 스킬)
4. UI 리뷰 (plan-design-review 스킬)
5. DX 리뷰 (plan-devex-review 스킬)
6. Plan 문서를 .hermes/plans/plan_YYYYMMDD.md에 생성하세요
7. main에서 새 브랜치를 생성하세요 (feature/ 형식)
8. Plan 문서를 커밋하세요
9. 브랜치명을 .hermes/pipeline_branch.txt에 저장하세요
10. TODOS.md의 해당 작업을 처리중으로 표시하세요""",
            "tools": "Read,Write,Bash",
            "turns": 20,
        },
        {
            "num": 3,
            "name": "Writing Plan",
            "prompt": """superpowers writing-plan 스킬을 사용하세요:
.hermes/plans/의 plan 문서를 superpowers 형식으로 변환하세요.""",
            "tools": "Read,Write,Bash",
            "turns": 15,
        },
        {
            "num": 4,
            "name": "Development",
            "prompt": """superpowers subagent-driven-development 스킬을 사용하세요:
Plan 문서를 따라 코드를 구현하세요. 모든 테스트가 통과해야 합니다.""",
            "tools": "Read,Write,Edit,Bash",
            "turns": 50,
        },
        {
            "num": 5,
            "name": "Review",
            "prompt": """gstack review 스킬을 사용하세요:
구현된 결과를 리뷰하세요.""",
            "tools": "Read,Bash",
            "turns": 15,
        },
        {
            "num": "6.1",
            "name": "CSO Security Review",
            "prompt": """gstack cso 스킬을 사용하세요.

현재 브랜치의 보안 리뷰를 수행하세요.
리포트가 있다면 .hermes/security-report.json에 저장하세요.""",
            "tools": "Read,Write,Bash",
            "turns": 20,
        },
        {
            "num": "6.2",
            "name": "QA Test",
            "prompt": """gstack qa 스킬을 사용하세요.

현재 브랜치의 QA 테스트를 수행하세요.
결과가 있다면 .hermes/qa-report.json에 저장하세요.""",
            "tools": "Read,Write,Bash",
            "turns": 15,
        },
        {
            "num": "6.3",
            "name": "Design/UI Review",
            "prompt": """gstack design-review 스킬을 사용하세요.

현재 브랜치의 UI/디자인 리뷰를 수행하세요.
결과가 있다면 .hermes/design-review-report.json에 저장하세요.""",
            "tools": "Read,Write,Bash",
            "turns": 15,
        },
        {
            "num": "6.4",
            "name": "DX Review",
            "prompt": """gstack devex-review 스킬을 사용하세요.

현재 브랜치의 개발자 경험 리뷰를 수행하세요.
결과가 있다면 .hermes/dx-review-report.json에 저장하세요.""",
            "tools": "Read,Write,Bash",
            "turns": 15,
        },
        {
            "num": 7,
            "name": "Document Release",
            "prompt": """릴리즈 문서를 생성하세요.

1. CHANGELOG.md를 생성 또는 업데이트하세요 (현재 브랜치의 변경사항 포함)
2. RELEASE_NOTES.md를 생성 또는 업데이트하세요 (v0.1.0 또는 다음 버전)
3. README.md의 프로젝트 구조를 업데이트하세요 (새 파일 추가 반영)
4. 변경사항을 커밋하세요

gstack document-release 스킬은 AskUserQuestion을 필요로 하므로 직접 수행하세요.""",
            "tools": "Read,Write,Edit,Bash",
            "turns": 15,
        },
        {
            "num": 8,
            "name": "Finish Branch",
            "prompt": """superpowers finish-a-development-branch 스킬을 사용하세요:
브랜치를 main에 머지하고 마무리하세요.""",
            "tools": "Read,Write,Bash",
            "turns": 10,
        },
    ]

    # 마지막 완료된 Phase 확인 (인덱스 기반)
    last_completed_idx = -1
    for idx, phase in enumerate(phases):
        phase_num = phase["num"]
        checkpoint_file = os.path.join(CKPT_DIR, f"phase_{phase_num}_done")
        if os.path.exists(checkpoint_file):
            last_completed_idx = idx

    if last_completed_idx >= 0:
        print(f"✓ Phase 2~{phases[last_completed_idx]['num']} 완료 (체크포인트에서 재개)")

    # Phase 실행
    branch = "unknown"
    for idx, phase in enumerate(phases):
        if idx <= last_completed_idx:
            print(f"⏭ Phase {phase['num']} ({phase['name']}) 스킵")
            continue

        print(f"▶ Phase {phase['num']}: {phase['name']} 시작")

        # Phase 시작 알림
        try:
            send_slack(channel, f"▶ Phase {phase['num']} ({phase['name']}) 시작")
        except Exception as e:
            print(f"  ⚠ Slack 알림 실패: {e}")

        result = run_phase(phase["num"], phase["name"], phase["prompt"], phase["tools"], phase["turns"])

        if result.returncode != 0:
            # 실패 시 슬랙 알림
            try:
                send_slack(channel, f"❌ Phase {phase['num']} ({phase['name']}) 실패\n로그: {project_dir}/{LOG_FILE}")
            except:
                pass
            print(f"FAIL Phase {phase['num']} ({phase['name']})")
            sys.exit(1)

        # 체크포인트 저장
        with open(os.path.join(CKPT_DIR, f"phase_{phase['num']}_done"), "w") as f:
            f.write(f"completed_at: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")

        # Phase 2 완료 시 브랜치명 확인
        if phase["num"] == 2:
            if os.path.exists(BRANCH_FILE):
                with open(BRANCH_FILE) as f:
                    branch = f.read().strip()
                try:
                    send_slack(channel, f"✅ Phase 2 완료 — 브랜치: {branch}")
                except:
                    pass
            else:
                try:
                    send_slack(channel, f"✅ Phase 2 완료")
                except:
                    pass
        else:
            # 완료 알림
            try:
                send_slack(channel, f"✅ Phase {phase['num']} ({phase['name']}) 완료")
            except:
                pass

    # 전체 완료
    save_hash(project, todo_hash)

    try:
        send_slack(channel, f"🎉 {project} 파이프라인 전체 완료! 브랜치: {branch}")
    except:
        pass

    print(f"✅ Pipeline 완료 (브랜치: {branch})")

if __name__ == "__main__":
    main()
