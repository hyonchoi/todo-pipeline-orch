"""Supervisor-owned progress. Staged agent claims never constitute evidence.

``record_receipt`` is a control-plane API: callers must have actually collected
verification or independent coordinator review for the exact commit. It is
deliberately unavailable through the agent-facing supervisor command interface.
Only a per-attempt staging directory should be added to client write permissions.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from .agent_execution import ExecutionError, _atomic_write, _open_directory, _safe_read

_DIGEST = re.compile(r'[0-9a-f]{64}\Z')
_COMMIT = re.compile(r'[0-9a-f]{40}(?:[0-9a-f]{24})?\Z')
_SUBMISSION_FIELDS = {'version', 'execution_id', 'generation', 'plan_identity', 'task_id', 'commit'}
_FIELDS = {'version', 'execution_id', 'plan_identity', 'manifest_digest', 'task_ids', 'base', 'accepted', 'receipts'}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class ProgressJournal:
    def __init__(self, store, execution_id: str):
        self.store = store
        self.execution_id = execution_id

    def _registration(self):
        return self.store.load(self.execution_id)['registration']

    def _git(self, *arguments):
        try:
            result = subprocess.run(['git', '-C', self._registration()['worktree'], *arguments],
                                    capture_output=True, timeout=30, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExecutionError('checkpoint Git validation failed') from exc
        return result.stdout.decode('utf-8', errors='surrogateescape').strip()

    def _write(self, journal):
        with self.store._directory_handle(self.execution_id) as directory:
            _atomic_write(Path('progress.json'), journal, directory_fd=directory)
            # A crash between these fsynced replacements deliberately blocks
            # recovery. Never silently accept a truncated or rewritten history.
            _atomic_write(Path('progress-anchor.json'), {'version': 1, 'digest': _digest(journal)}, directory_fd=directory)

    def _load(self):
        with self.store._directory_handle(self.execution_id) as directory:
            try:
                journal = json.loads(_safe_read(Path('progress.json'), directory_fd=directory))
            except (ValueError, UnicodeError) as exc:
                raise ExecutionError('invalid progress journal') from exc
        registration = self._registration()
        if not isinstance(journal, dict) or set(journal) != _FIELDS or type(journal['version']) is not int or journal['version'] != 1:
            raise ExecutionError('unsupported progress schema')
        if journal['execution_id'] != self.execution_id or journal['plan_identity'] != registration['plan_identity'] or journal['manifest_digest'] != _digest(registration['manifest']) or journal['task_ids'] != self._tasks(registration):
            raise ExecutionError('checkpoint Plan drift')
        if registration['result_contract'].get('base_sha') and journal['base'] != registration['result_contract']['base_sha']:
            raise ExecutionError('rewritten checkpoint base')
        if not isinstance(journal['accepted'], list) or not isinstance(journal['receipts'], list) or not isinstance(journal['base'], str) or not _COMMIT.fullmatch(journal['base']):
            raise ExecutionError('invalid progress history')
        for receipt in journal['receipts']:
            self._validate_receipt(receipt, journal)
        previous = journal['base']
        for index, entry in enumerate(journal['accepted']):
            if not isinstance(entry, dict) or set(entry) != {'task_id', 'commit', 'generation', 'previous', 'digest'} or index >= len(journal['task_ids']) or entry['task_id'] != journal['task_ids'][index] or entry['previous'] != previous or entry['digest'] != _digest({k: v for k, v in entry.items() if k != 'digest'}):
                raise ExecutionError('rewritten checkpoint history')
            self._require_receipts(journal, entry['generation'], entry['task_id'], entry['commit'])
            if entry['commit'] == previous:
                raise ExecutionError('checkpoint must advance commit history')
            self._sole_parent(previous, entry['commit'])
            previous = entry['commit']
        with self.store._directory_handle(self.execution_id) as directory:
            try:
                anchor = json.loads(_safe_read(Path('progress-anchor.json'), directory_fd=directory))
            except (FileNotFoundError, ValueError, UnicodeError) as exc:
                raise ExecutionError('rewritten or interrupted checkpoint anchor') from exc
        if not isinstance(anchor, dict) or set(anchor) != {'version', 'digest'} or type(anchor['version']) is not int or anchor['version'] != 1 or anchor['digest'] != _digest(journal):
            raise ExecutionError('rewritten or interrupted checkpoint history')
        return journal

    @staticmethod
    def _tasks(registration):
        manifest = registration['manifest']
        if manifest is None:
            return []
        tasks = manifest.get('tasks')
        if not isinstance(tasks, list) or not tasks or any(not isinstance(task, dict) or not isinstance(task.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', task['id']) for task in tasks):
            raise ExecutionError('invalid pinned checkpoint manifest')
        ids = [task['id'] for task in tasks]
        if len(ids) != len(set(ids)):
            raise ExecutionError('duplicate checkpoint task')
        return ids

    def initialize(self):
        with self.store.locked(self.execution_id):
            try:
                return self._load()
            except FileNotFoundError:
                self._reject_lost_progress(initializing=True)
                attempts = self.store.load(self.execution_id)['attempts']
                if attempts:
                    raise ExecutionError('legacy progress absent: recovery-only validation required')
                registration = self._registration()
                journal = dict(version=1, execution_id=self.execution_id,
                               plan_identity=registration['plan_identity'],
                               manifest_digest=_digest(registration['manifest']),
                               task_ids=self._tasks(registration), base=registration['result_contract'].get('base_sha') or self._git('rev-parse', 'HEAD'),
                               accepted=[], receipts=[])
                self._check_git(journal)
                self._write(journal)
                return journal

    def _reject_lost_progress(self, *, initializing=False):
        registration = self._registration()
        with self.store._directory_handle(self.execution_id) as directory:
            try:
                _safe_read(Path('progress-anchor.json'), directory_fd=directory)
            except FileNotFoundError:
                pass
            else:
                raise ExecutionError('missing modern progress journal')
        if registration['result_contract'].get('progress_version') is not None:
            if not initializing or self.store.load(self.execution_id)['attempts']:
                raise ExecutionError('missing modern progress journal')

    def validate_fresh(self):
        """Check pinned Git state before admitting the first client process."""
        with self.store.locked(self.execution_id):
            journal = self._load()
            head, previous = self._check_git(journal)
            if journal['task_ids'] and (head != previous or self._git('status', '--porcelain=v1', '-z')):
                raise ExecutionError('fresh execution requires clean pinned base')
            return head, previous

    def _attempt(self, generation):
        attempts = self.store.load(self.execution_id)['attempts']
        if type(generation) is not int or not attempts or generation != attempts[-1]['generation']:
            raise ExecutionError('stale checkpoint attempt')

    def staging_directory(self, generation: int) -> Path:
        self._attempt(generation)
        directory = self.store.root.parent / 'agent-submissions' / self.execution_id / str(generation)
        worktree = Path(self._registration()['worktree']).absolute()
        if directory == worktree or worktree in directory.parents or directory == self.store.root or self.store.root in directory.parents:
            raise ExecutionError('checkpoint staging must be outside authority and worktree')
        with _open_directory(directory, create=True):
            pass
        return directory

    def _check_git(self, journal):
        if self._git('symbolic-ref', '--short', 'HEAD') != self._registration()['branch']:
            raise ExecutionError('checkpoint branch drift')
        if any(entry and (entry[0].islower() or entry[0] == 'S')
               for entry in self._git('ls-files', '-v', '-z').split('\0')):
            raise ExecutionError('checkpoint hidden index flags block validation')
        for name in ('index.lock', 'HEAD.lock', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-apply', 'rebase-merge', 'sequencer'):
            location = Path(self._git('rev-parse', '--git-path', name))
            if not location.is_absolute():
                location = Path(self._registration()['worktree']) / location
            if location.exists() or location.is_symlink():
                raise ExecutionError('checkpoint Git lock or merge state')
        head = self._git('rev-parse', 'HEAD')
        previous = journal['accepted'][-1]['commit'] if journal['accepted'] else journal['base']
        self._git('merge-base', '--is-ancestor', previous, head)
        return head, previous

    def _validate_receipt(self, receipt, journal):
        fields = {'generation', 'task_id', 'commit', 'kind', 'evidence_sha256', 'evidence', 'plan_identity'}
        if not isinstance(receipt, dict) or set(receipt) != fields or type(receipt['generation']) is not int or receipt['generation'] < 1 or receipt['task_id'] not in journal['task_ids'] or receipt['kind'] not in {'verification', 'review'} or receipt['plan_identity'] != journal['plan_identity'] or not isinstance(receipt['commit'], str) or not _COMMIT.fullmatch(receipt['commit']) or not isinstance(receipt['evidence_sha256'], str) or not _DIGEST.fullmatch(receipt['evidence_sha256']):
            raise ExecutionError('invalid control receipt')
        self._validate_evidence(receipt['kind'], receipt['evidence'])
        if receipt['evidence_sha256'] != _digest(receipt['evidence']):
            raise ExecutionError('control receipt evidence digest mismatch')

    @staticmethod
    def _validate_evidence(kind, evidence):
        if not isinstance(evidence, dict) or len(json.dumps(evidence)) > 16384:
            raise ExecutionError('invalid bounded control evidence')
        if kind == 'review':
            if set(evidence) != {'reviewer', 'receipt_id', 'outcome'} or evidence['outcome'] != 'accepted' or any(not isinstance(evidence[key], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', evidence[key]) for key in ('reviewer', 'receipt_id')):
                raise ExecutionError('invalid independent review evidence')
        elif kind == 'verification':
            if set(evidence) != {'checks'} or not isinstance(evidence['checks'], list) or not 1 <= len(evidence['checks']) <= 64:
                raise ExecutionError('invalid verification evidence')
            for check in evidence['checks']:
                if not isinstance(check, dict) or set(check) != {'argv', 'exit_code'} or type(check['exit_code']) is not int or check['exit_code'] != 0 or not isinstance(check['argv'], list) or not 1 <= len(check['argv']) <= 64 or any(not isinstance(arg, str) or not re.fullmatch(r'[A-Za-z0-9_./:=+-]{1,256}', arg) or re.search(r'token|secret|authorization|password|api.key|bearer', arg, re.I) for arg in check['argv']):
                    raise ExecutionError('invalid or sensitive verification evidence')
        else:
            raise ExecutionError('unsupported evidence kind')

    def _sole_parent(self, previous, commit):
        if self._git('rev-list', '--parents', '-n', '1', commit).split() != [commit, previous]:
            raise ExecutionError('checkpoint commit must have previous commit as sole parent')

    def record_receipt(self, generation, task_id, commit, *, kind, evidence):
        """Record already-collected trusted evidence; never call with worker claims."""
        with self.store.locked(self.execution_id):
            self._attempt(generation)
            journal = self._load()
            head, _ = self._check_git(journal)
            receipt = dict(generation=generation, task_id=task_id, commit=commit, kind=kind,
                           evidence_sha256=_digest(evidence), evidence=evidence, plan_identity=journal['plan_identity'])
            self._validate_receipt(receipt, journal)
            self._git('merge-base', '--is-ancestor', commit, head)
            if receipt not in journal['receipts']:
                journal['receipts'].append(receipt)
                self._write(journal)

    @staticmethod
    def _require_receipts(journal, generation, task_id, commit):
        kinds = {r['kind'] for r in journal['receipts'] if r['generation'] == generation and r['task_id'] == task_id and r['commit'] == commit}
        if kinds != {'verification', 'review'}:
            raise ExecutionError('trusted verification and coordinator review receipts required')

    def promote(self, generation: int, filename: str):
        with self.store.locked(self.execution_id):
            self._attempt(generation)
            if not isinstance(filename, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}\.json', filename):
                raise ExecutionError('invalid checkpoint submission path')
            raw = _safe_read(self.staging_directory(generation) / filename)
            if len(raw) > 16384:
                raise ExecutionError('checkpoint evidence exceeds size limit')
            try:
                submission = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise ExecutionError('invalid checkpoint submission') from exc
            journal = self._load()
            if not isinstance(submission, dict) or set(submission) != _SUBMISSION_FIELDS or type(submission['version']) is not int or submission['version'] != 1 or type(submission['generation']) is not int or submission['generation'] != generation or submission['execution_id'] != self.execution_id or submission['plan_identity'] != journal['plan_identity'] or not isinstance(submission['commit'], str) or not _COMMIT.fullmatch(submission['commit']):
                raise ExecutionError('invalid checkpoint submission identity or schema')
            index = len(journal['accepted'])
            if index >= len(journal['task_ids']) or submission['task_id'] != journal['task_ids'][index]:
                raise ExecutionError('checkpoint task order mismatch')
            head, previous = self._check_git(journal)
            commit = submission['commit']
            if commit == previous:
                raise ExecutionError('checkpoint must advance commit history')
            self._sole_parent(previous, commit)
            self._git('merge-base', '--is-ancestor', commit, head)
            self._require_receipts(journal, generation, submission['task_id'], commit)
            entry = dict(task_id=submission['task_id'], commit=commit, generation=generation, previous=previous)
            entry['digest'] = _digest(entry)
            journal['accepted'].append(entry)
            self._write(journal)
            return journal

    def recovery_context(self, generation: int):
        with self.store.locked(self.execution_id):
            self._attempt(generation)
            legacy = False
            try:
                journal = self._load()
            except FileNotFoundError:
                self._reject_lost_progress()
                legacy = True
                journal = dict(base=self._git('rev-parse', 'HEAD'), accepted=[], task_ids=[],
                               plan_identity=self._registration()['plan_identity'])
            head, previous = self._check_git(journal)
            candidates = self._git('rev-list', '--reverse', f'{previous}..{head}').splitlines()
            status = self._git('status', '--porcelain=v1', '-z')
            count = len(journal['accepted'])
            tasks = journal['task_ids']
            if tasks and len(candidates) > len(tasks) - count:
                raise ExecutionError('unexpected HEAD beyond unfinished task history')
            candidate_parent = previous
            for candidate in candidates:
                self._sole_parent(candidate_parent, candidate)
                candidate_parent = candidate
            mode = 'resume'
            if not tasks or candidates:
                mode = 'recovery_validation'
            elif count == len(tasks):
                mode = 'verification_only'
            return dict(version=1, mode=mode, plan_identity=journal['plan_identity'],
                        worktree=self._registration()['worktree'], branch=self._registration()['branch'],
                        head=head, accepted=journal['accepted'], current_task=tasks[count] if count < len(tasks) else None,
                        candidate_commits=candidates, dirty=bool(status),
                        status_sha256=hashlib.sha256(status.encode(errors='surrogateescape')).hexdigest(),
                        subtask_guarantee=bool(tasks), legacy_evidence_absent=legacy,
                        instruction='Preserve and inspect all staged, unstaged and untracked work. Validate and independently review candidate commits before checkpointing. Never reset, clean, or commit incomplete work.')
