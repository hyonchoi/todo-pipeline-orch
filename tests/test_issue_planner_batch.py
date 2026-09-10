import copy
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

WRITER = Path(__file__).parents[1] / (
    "hermes_pipeline/data/skills/todo-manager/scripts/write_request.py"
)


@pytest.fixture
def writer():
    spec = importlib.util.spec_from_file_location("batch_writer", WRITER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode()).hexdigest()


def seal(record):
    packet = {k: v for k, v in record.items() if k not in {"reviews", "approval_digest"}}
    for review in record["reviews"]:
        review["packet_digest"] = digest(packet)
    record["approval_digest"] = digest({
        k: v for k, v in record.items() if k != "approval_digest"
    })
    return record


def batch(group=True):
    batch_id = str(uuid.uuid4())
    keys = ["implementation", "validation"] if group else ["implementation"]
    record = {
        "schema_version": 1, "batch_id": batch_id, "repository": "acme/demo",
        "source_plan_sha256": "a" * 64, "strategy": "incremental",
        "manual_handoff": False,
        "parent": {
            "key": "goal", "title": "Original goal",
            "transaction_marker": f"<!-- issue-planner-batch: {batch_id} -->",
            "body": "Original goal and delivery contract",
            "body_sha256": hashlib.sha256(b"Original goal and delivery contract").hexdigest(),
        } if group else None,
        "issues": [{
            "key": key, "title": key.title(), "parent_key": "goal" if group else None,
            "transaction_id": str(uuid.uuid4()),
            "request_sha256": str(index + 1) * 64,
            "body": "Approved implementation preview",
            "body_sha256": hashlib.sha256(b"Approved implementation preview").hexdigest(),
            "role": "final-validation" if key == "validation" else "implementation",
            "size": "Small", "hold": True,
        } for index, key in enumerate(keys)],
        "dependencies": [{"issue_key": "validation", "requires": "implementation"}]
        if group else [],
        "terminal_validator": "validation" if group else None,
        "reviews": [{
            "engine": engine, "verdict": "PASS", "issue_keys":
            (["goal"] if group else []) + keys, "packet_digest": "0" * 64,
            "route": "read-only", "evidence_sha256": "c" * 64,
        } for engine in ["codex", "claude"]],
        "exceptions": [],
    }
    for issue in record["issues"]:
        issue["request_sha256"] = hashlib.sha256(request_bytes(issue)).hexdigest()
    if record["parent"]:
        record["parent"]["body"] = record["parent"]["transaction_marker"] + "\nOriginal goal and delivery contract"
        record["parent"]["body_sha256"] = hashlib.sha256(record["parent"]["body"].encode()).hexdigest()
    return seal(record)


def request_bytes(issue):
    return json.dumps({"transaction_id": issue["transaction_id"],
                       "title": issue["title"], "hold": True}).encode()


def write(writer, project, record):
    for issue in record["issues"]:
        path = project / ".hermes" / "todo-create-input" / f"{issue['transaction_id']}.json"
        if not path.exists():
            writer.write_request(project, issue["transaction_id"], request_bytes(issue))
    return writer.write_batch(project, record["batch_id"], json.dumps(record).encode())


@pytest.mark.parametrize("group", [False, True])
def test_batch_is_private_immutable_and_preserves_approved_bytes(writer, tmp_path, group):
    record = batch(group)
    path = write(writer, tmp_path, record)
    assert path.parent.name == "issue-planner-batches"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    original = path.read_bytes()
    assert json.loads(original) == record
    with pytest.raises(FileExistsError):
        write(writer, tmp_path, record)
    assert path.read_bytes() == original


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(schema_version=True),
    lambda r: r.update(manual_handoff=1),
    lambda r: r.update(strategy="integration"),
    lambda r: r["issues"][0].update(hold=False),
    lambda r: r["issues"][0].update(hold=1),
    lambda r: r["issues"][0].update(parent_key="other"),
    lambda r: r["issues"][0].update(request_sha256="invalid"),
    lambda r: r["issues"][0].update(transaction_id=r["issues"][1]["transaction_id"]),
    lambda r: r["issues"][0].update(key="validation"),
    lambda r: r["issues"][0].update(size="Large"),
    lambda r: r["parent"].update(transaction_marker="wrong"),
    lambda r: r["parent"].update(labels=["tpo:todo"]),
    lambda r: r.update(dependencies=[]),
    lambda r: r["dependencies"].append({"issue_key": "implementation", "requires": "validation"}),
    lambda r: r["dependencies"].append({"issue_key": "validation", "requires": "goal"}),
    lambda r: r["dependencies"].append(copy.deepcopy(r["dependencies"][0])),
    lambda r: r.update(terminal_validator="implementation"),
    lambda r: r["reviews"][0].update(verdict="FAIL"),
    lambda r: r["reviews"][0].update(issue_keys=["goal"]),
    lambda r: r["reviews"][0].update(raw_output="secret provider response"),
    lambda r: r.update(exceptions=[{"kind": "invented"}]),
])
def test_invalid_identity_graph_and_review_records_fail_before_writing(writer, tmp_path, mutate):
    record = batch()
    mutate(record)
    seal(record)
    with pytest.raises(SystemExit):
        write(writer, tmp_path, record)
    assert not (tmp_path / ".hermes" / "issue-planner-batches").exists()


def test_stale_approval_or_review_digest_rejected(writer, tmp_path):
    record = batch()
    record["issues"][0]["title"] = "Changed intent"
    with pytest.raises(SystemExit):
        write(writer, tmp_path, record)
    record["approval_digest"] = digest({
        k: v for k, v in record.items() if k != "approval_digest"
    })
    with pytest.raises(SystemExit):
        write(writer, tmp_path, record)


@pytest.mark.parametrize("target", [".hermes", ".hermes/issue-planner-batches"])
def test_symlink_directory_rejected(writer, tmp_path, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    destination = tmp_path / target
    destination.parent.mkdir(exist_ok=True)
    destination.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SystemExit):
        write(writer, tmp_path, batch())
    assert not list(outside.iterdir())


def test_batch_creation_race_has_one_winner(writer, tmp_path):
    record = batch()
    for issue in record["issues"]:
        writer.write_request(tmp_path, issue["transaction_id"], request_bytes(issue))
    def attempt(_):
        try:
            return write(writer, tmp_path, record)
        except FileExistsError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(attempt, range(4)))
    assert sum(path is not None for path in paths) == 1


def test_interrupted_write_is_retained_and_cannot_be_replaced(writer, tmp_path, monkeypatch):
    record = batch()
    for issue in record["issues"]:
        writer.write_request(tmp_path, issue["transaction_id"], request_bytes(issue))
    original_fsync = writer.os.fsync
    def interrupted(fd):
        if stat.S_ISREG(writer.os.fstat(fd).st_mode):
            raise OSError("simulated interruption")
        original_fsync(fd)
    monkeypatch.setattr(writer.os, "fsync", interrupted)
    with pytest.raises(OSError):
        write(writer, tmp_path, record)
    monkeypatch.setattr(writer.os, "fsync", original_fsync)
    with pytest.raises(FileExistsError):
        write(writer, tmp_path, record)


def test_directory_swap_is_detected_before_record_creation(writer, tmp_path, monkeypatch):
    record = batch()
    for issue in record["issues"]:
        writer.write_request(tmp_path, issue["transaction_id"], request_bytes(issue))
    def swap(point):
        if point == "input-opened":
            directory = tmp_path / ".hermes" / "issue-planner-batches"
            directory.rename(directory.with_name("detached"))
            directory.mkdir(mode=0o700)
    monkeypatch.setattr(writer, "_checkpoint", swap)
    with pytest.raises(SystemExit):
        write(writer, tmp_path, record)
    assert not list((tmp_path / ".hermes" / "detached").iterdir())


def test_missing_approved_request_stops_batch(writer, tmp_path):
    record = batch()
    with pytest.raises((SystemExit, FileNotFoundError)):
        writer.write_batch(tmp_path, record["batch_id"], json.dumps(record).encode())
    assert not (tmp_path / ".hermes" / "issue-planner-batches").exists()


@pytest.mark.parametrize("change", ["hash", "title", "hold", "symlink", "mode"])
def test_approved_request_identity_is_verified(writer, tmp_path, change):
    record = batch()
    for issue in record["issues"]:
        writer.write_request(tmp_path, issue["transaction_id"], request_bytes(issue))
    issue = record["issues"][0]
    path = tmp_path / ".hermes" / "todo-create-input" / f"{issue['transaction_id']}.json"
    payload = json.loads(path.read_bytes())
    if change in {"hash", "title", "hold"}:
        payload.update({"hash": {"extra": True}, "title": {"title": "Wrong"},
                        "hold": {"hold": False}}[change])
        path.write_text(json.dumps(payload))
        if change != "hash":
            issue["request_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            seal(record)
    elif change == "symlink":
        target = path.with_suffix(".actual")
        path.rename(target)
        path.symlink_to(target)
    else:
        path.chmod(0o644)
    with pytest.raises((SystemExit, OSError)):
        writer.write_batch(tmp_path, record["batch_id"], json.dumps(record).encode())


def test_integration_batch_preserves_manual_hold_contract(writer, tmp_path):
    record = batch()
    record.update(strategy="integration", manual_handoff=True)
    path = write(writer, tmp_path, seal(record))
    assert json.loads(path.read_bytes())["manual_handoff"] is True
    assert all(issue["hold"] for issue in json.loads(path.read_bytes())["issues"])


def test_disclosed_unavailable_engine_and_atomic_large_exception(writer, tmp_path):
    record = batch()
    record["issues"][0]["size"] = "Large"
    record["reviews"] = record["reviews"][:1]
    record["exceptions"] = [
        {"kind": "atomic-large", "issue_key": "implementation",
         "rationale_sha256": "d" * 64, "permission_sha256": "e" * 64},
        {"kind": "reduced-review", "unavailable_engine": "claude",
         "disclosure_sha256": "f" * 64, "alternate_route_sha256": "a" * 64},
    ]
    assert write(writer, tmp_path, seal(record)).exists()


def test_explicit_batch_cli_preserves_legacy_request_interface(writer, tmp_path):
    record = batch(False)
    issue = record["issues"][0]
    writer.write_request(tmp_path, issue["transaction_id"], request_bytes(issue))
    result = subprocess.run(
        [sys.executable, str(WRITER), "--batch", str(tmp_path), record["batch_id"]],
        input=json.dumps(record), text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).read_text() == json.dumps(record)


def test_duplicate_json_keys_are_rejected(writer, tmp_path):
    record = batch(False)
    raw = json.dumps(record).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    with pytest.raises(SystemExit, match="duplicate keys"):
        writer.write_batch(tmp_path, record["batch_id"], raw.encode())


@pytest.mark.parametrize("change", ["missing", "duplicate", "manifest", "prefix", "suffix"])
def test_parent_body_must_preserve_nonexecutable_recovery_identity(writer, tmp_path, change):
    record = batch()
    parent = record["parent"]
    if change == "missing":
        parent["body"] = "No recovery marker"
    elif change == "duplicate":
        parent["body"] += "\n" + parent["transaction_marker"]
    elif change == "manifest":
        parent["body"] += '\n```json tpo-plan\n{"schema_version": 1}\n```'
    elif change == "prefix":
        parent["body"] = "Inline prefix " + parent["body"]
    else:
        parent["body"] = parent["body"].replace(parent["transaction_marker"], parent["transaction_marker"] + " inline suffix")
    parent["body_sha256"] = hashlib.sha256(parent["body"].encode()).hexdigest()
    seal(record)
    with pytest.raises(SystemExit):
        write(writer, tmp_path, record)


def test_fifo_approved_request_is_rejected_without_blocking(tmp_path):
    record = batch(False)
    directory = tmp_path / ".hermes" / "todo-create-input"
    directory.mkdir(parents=True, mode=0o700)
    os.mkfifo(directory / f"{record['issues'][0]['transaction_id']}.json", 0o600)
    result = subprocess.run(
        [sys.executable, str(WRITER), "--batch", str(tmp_path), record["batch_id"]],
        input=json.dumps(record), text=True, capture_output=True, timeout=2,
    )
    assert result.returncode != 0
    assert "private regular file" in result.stderr
    assert not (tmp_path / ".hermes" / "issue-planner-batches").exists()


def test_oversized_retained_request_is_rejected_before_reading(writer, tmp_path):
    record = batch(False)
    issue = record["issues"][0]
    path = writer.write_request(tmp_path, issue["transaction_id"], request_bytes(issue))
    with path.open("ab") as handle:
        handle.truncate(4 * 1024 * 1024 + 1)
    with pytest.raises(SystemExit, match="size limit"):
        writer.write_batch(tmp_path, record["batch_id"], json.dumps(record).encode())
