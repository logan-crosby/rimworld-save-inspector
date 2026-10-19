"""Read-only authentication of final-candidate G/H/L/A acceptance evidence.

This tool never runs producers, accesses saves, registers hosts or publishes.
Validator self-tests cannot return product acceptance. Semantic proof comes from
required assertion producers and independently reproduced review, not receipt shape.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET

from tools.gates.complete_contract import (REQUIRED_CASES, REVIEW_AREAS,
    FROZEN_REGISTRY_CANONICAL_SHA256, FROZEN_PROOF_MAP_CANONICAL_SHA256,
    EXPECTED_CHECK_KINDS)

GATES = {f'{family}{n}' for family in 'GHLA' for n in range(8)}
CANDIDATE_KEYS = {'commit', 'source_tree_sha256', 'wheel_sha256', 'sdist_sha256', 'recorder_sha256'}
SOURCE_ROOT = Path(__file__).resolve().parents[1]
PROOF_MAP_PATH = Path(__file__).resolve().parents[1] / 'docs/requirements/acceptance-proof-map.json'


@contextmanager
def _open_beneath(root: Path, value: str):
    """Open a regular file relative to held directory descriptors, refusing symlinks."""
    parts = PurePosixPath(value).parts
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as stack:
        parent = os.open(root, directory_flags)
        stack.callback(os.close, parent)
        for component in parts[:-1]:
            parent = os.open(component, directory_flags, dir_fd=parent)
            stack.callback(os.close, parent)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        stack.callback(os.close, fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('artifact is not a regular file')
        yield fd


def _digest_fd(fd: int, *, retain: bool = False, max_bytes: int | None = None):
    digest = hashlib.sha256()
    chunks = [] if retain else None
    size = 0
    while chunk := os.read(fd, 1024 * 1024):
        size += len(chunk)
        if max_bytes is not None and size > max_bytes:
            raise ValueError('artifact size differs')
        digest.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
    return size, digest.hexdigest(), b''.join(chunks) if chunks is not None else None


def source_tree_fingerprint(source_root: Path | None = None) -> str:
    """Fingerprint every tracked candidate file by path, exact bytes and size."""
    source_root = SOURCE_ROOT if source_root is None else source_root
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=source_root).decode().split('\0')
    rows = []
    for name in sorted(x for x in names if x):
        _path(source_root, name)
        with _open_beneath(source_root, name) as fd:
            size, digest, _ = _digest_fd(fd)
        rows.append([name, size, digest])
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _product_inputs(root, bundle, candidate, registry):
    canonical = json.dumps(registry, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    if hashlib.sha256(canonical).hexdigest() != FROZEN_REGISTRY_CANONICAL_SHA256:
        raise ValueError('requirement scope differs from the independent A0 freeze')
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=SOURCE_ROOT, text=True).strip()
    dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=SOURCE_ROOT, text=True).strip()
    if dirty or head != candidate['commit'] or source_tree_fingerprint(SOURCE_ROOT) != candidate['source_tree_sha256']:
        raise ValueError('candidate source commit/tree is dirty, stale or mismatched')
    packages = bundle['packages']
    if set(packages) != {'wheel', 'sdist', 'recorder'}:
        raise ValueError('final package population differs')
    for name in ['wheel', 'sdist', 'recorder']:
        _artifact(root, packages[name])
        if packages[name]['sha256'] != candidate[name + '_sha256']:
            raise ValueError('candidate package hash differs from retained bytes')
    commit_stamp = subprocess.check_output(['git', 'show', '-s', '--format=%cI', 'HEAD'],
                                           cwd=SOURCE_ROOT, text=True).strip()
    commit_time = dt.datetime.fromisoformat(commit_stamp)
    if commit_time.tzinfo is None:
        raise ValueError('candidate commit has no timestamp offset')
    commit_time = commit_time.astimezone(dt.timezone.utc)
    if commit_time > dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5):
        raise ValueError('candidate commit timestamp is in the future')
    return commit_time


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def _json_bytes(raw: bytes):
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError('evidence JSON exceeds 32MiB')
    return json.loads(raw, object_pairs_hook=_unique_pairs)


def _json(path: Path):
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError('evidence JSON exceeds32MiB')
    return _json_bytes(path.read_bytes())


def _candidate(value):
    return (isinstance(value, dict) and set(value) == CANDIDATE_KEYS
        and isinstance(value['commit'], str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value['commit'])
        and all(isinstance(value[k], str) and re.fullmatch(r'[0-9a-f]{64}', value[k]) for k in CANDIDATE_KEYS - {'commit'}))


def _path(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('invalid artifact path')
    rel = PurePosixPath(value)
    if rel.is_absolute() or '..' in rel.parts or rel.as_posix() != value:
        raise ValueError('artifact path escapes or is not canonical')
    current = root
    for component in rel.parts:
        current /= component
        if current.is_symlink():
            raise ValueError('symlink evidence is not allowed')
    if not current.is_file() or not current.resolve().is_relative_to(root.resolve()):
        raise ValueError('missing or escaped artifact')
    return current


def _artifact(root: Path, ref: dict, *, read: bool = False):
    if not isinstance(ref, dict) or set(ref) != {'path', 'bytes', 'sha256'}:
        raise ValueError('invalid artifact reference')
    path = _path(root, ref['path'])
    if type(ref['bytes']) is not int or ref['bytes'] < 0:
        raise ValueError('artifact size differs')
    if not isinstance(ref['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', ref['sha256']):
        raise ValueError('invalid artifact digest')
    with _open_beneath(root, ref['path']) as fd:
        if read:
            if ref['bytes'] > 32 * 1024 * 1024:
                raise ValueError('parsed artifact exceeds 32MiB')
        size, digest, raw = _digest_fd(fd, retain=read, max_bytes=ref['bytes'])
    if size != ref['bytes']:
        raise ValueError('artifact size changed during authentication')
    if digest != ref['sha256']:
        raise ValueError('artifact hash differs')
    return raw if read else path


def _tests(raw: bytes) -> set[str]:
    if len(raw) > 32 * 1024 * 1024 or re.search(br'<!\s*(?:DOCTYPE|ENTITY)', raw, re.I):
        raise ValueError('unsafe or oversized test report')
    root = ET.fromstring(raw)
    if root.tag not in {'testsuite', 'testsuites'}:
        raise ValueError('expected JUnit report')
    ids = []
    for suite in [root, *[x for x in root.iter('testsuite') if x is not root]]:
        for key in ('failures', 'errors', 'skipped', 'disabled'):
            if int(suite.attrib.get(key, '0')) != 0:
                raise ValueError('failed or skipped required tests')
    for case in root.iter('testcase'):
        if not case.attrib.get('classname') or not case.attrib.get('name'):
            raise ValueError('test identity missing')
        if any(case.find(key) is not None for key in ('failure', 'error', 'skipped')):
            raise ValueError('failed or skipped required test')
        ids.append(case.attrib['classname'] + '::' + case.attrib['name'])
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('empty or duplicate test identities')
    for suite in root.iter():
        if suite.tag not in {'testsuite', 'testsuites'}:
            continue
        actual = len(list(suite.iter('testcase')))
        if int(suite.attrib.get('tests', actual)) != actual:
            raise ValueError('test count differs from actual identities')
    return set(ids)


def _time(value):
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() != dt.timedelta(0):
        raise ValueError('command timestamp must be UTC')
    return parsed


def _requirement_gates(row):
    """Derive the gate family named by the frozen acceptance-test field."""
    text = row['acceptance_test']
    gates = set(re.findall(r'\b[GHLA][0-7]\b', text))
    for family, start, end in re.findall(r'\b([GHLA])([0-7])-\1([0-7])\b', text):
        if int(end) < int(start):
            raise ValueError('reversed requirement gate range')
        gates.update(f'{family}{n}' for n in range(int(start), int(end) + 1))
    if not gates and row['id'].startswith(('DISC-', 'REC-')):
        gates.add('A5')
    if not gates:
        raise ValueError('mandatory requirement has no gate assignment')
    return gates


def _proof_map(registry):
    mapping = _json(PROOF_MAP_PATH)
    if not isinstance(mapping, dict):
        raise ValueError('requirement proof map must be an object')
    canonical = json.dumps(mapping, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    if hashlib.sha256(canonical).hexdigest() != FROZEN_PROOF_MAP_CANONICAL_SHA256:
        raise ValueError('requirement proof map differs from the reviewed freeze')
    if (mapping.get('schema') != 'rwsi-acceptance-proof-map/1'
            or mapping.get('registry_canonical_sha256') != FROZEN_REGISTRY_CANONICAL_SHA256):
        raise ValueError('requirement proof map has the wrong scope')
    rows = mapping['requirements']
    required_rows = {row['id']: row for row in registry['requirements'] if row['disposition'] == 'mandatory'}
    if not isinstance(rows, dict) or set(rows) != set(required_rows):
        raise ValueError('requirement proof map population differs')
    known = {check for checks in REQUIRED_CASES.values() for check in checks}
    for requirement_id, checks in rows.items():
        if (not isinstance(checks, list) or not checks or len(checks) != len(set(checks))
                or not set(checks) <= known
                or not _requirement_gates(required_rows[requirement_id]) <= {x.split('.', 1)[0] for x in checks}):
            raise ValueError('invalid frozen proof mapping for ' + requirement_id)
    return rows


def _identity(value):
    return value.strip().casefold() if isinstance(value, str) and value.strip() else None


def _verify_command_timing(receipts, validated, commit_time):
    if 'G1' not in validated or 'A7' not in validated:
        raise ValueError('candidate build or review receipt is missing')
    g1 = receipts['G1']
    build_check = next(check for check in g1['checks'] if check['id'] == 'G1.T33')
    build_command = next(command for command in g1['commands'] if command['id'] == build_check['command_id'])
    build_time = _time(build_command['finished_at'])
    if _time(build_command['started_at']) < commit_time or build_time < commit_time:
        raise ValueError('final package producer predates candidate commit')
    latest = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
    if build_time > latest:
        raise ValueError('final package producer timestamp is in the future')
    for gate in validated:
        receipt = receipts[gate]
        for command in receipt['commands']:
            checks = [check for check in receipt['checks'] if check['command_id'] == command['id']]
            if not checks:
                raise ValueError('producer command has no required check identity')
            baseline = all(EXPECTED_CHECK_KINDS[check['id']] == 'baseline_oracle' for check in checks)
            started = _time(command['started_at'])
            finished = _time(command['finished_at'])
            if started > finished or started > latest or finished > latest:
                raise ValueError('producer command interval is invalid or in the future')
            if 'reuse' in command:
                raise ValueError('product evidence reuse is disabled')
            if not baseline and (started < commit_time or (gate != 'G1' and started < build_time)):
                raise ValueError('producer command predates final candidate or package')


def verify_bundle(root: Path, bundle: dict, candidate: dict, registry: dict,
                  contract: dict | None = None) -> dict:
    """Verify a bundle against externally supplied candidate and scope contracts."""
    root = root.resolve()
    if not isinstance(bundle, dict):
        return {'schema': 'rwsi-aggregate-result/1', 'status': 'FAIL', 'product_accepted': False,
            'candidate': candidate, 'validated_gates': [], 'missing_gates': sorted(GATES),
            'errors': ['aggregate bundle must be an object']}
    required = REQUIRED_CASES if contract is None else contract
    errors, missing, validated = [], [], []
    passed_checks = set()
    passing_by_gate = {}
    proof_map = None
    commit_time = None
    command_ids, log_paths = set(), set()
    if not _candidate(candidate) or bundle.get('candidate') != candidate:
        errors.append('external candidate identity mismatch or invalid')
    if bundle.get('schema') != 'rwsi-complete-acceptance/1':
        errors.append('unsupported aggregate schema')
    selftest = bundle.get('purpose') == 'validator_selftest'
    if bundle.get('purpose') not in {'validator_selftest', 'product_acceptance'}:
        errors.append('invalid acceptance purpose')
    if not selftest and contract is not None:
        errors.append('product acceptance cannot override the frozen gate contract')
    if not selftest:
        try:
            commit_time = _product_inputs(root, bundle, candidate, registry)
            proof_map = _proof_map(registry)
        except (OSError, ValueError, KeyError, TypeError, AttributeError,
                subprocess.CalledProcessError) as exc:
            errors.append('candidate/scope authentication: ' + str(exc))
    receipts = {}
    gate_refs = bundle.get('gates')
    if not isinstance(gate_refs, list):
        errors.append('gate receipt population must be a list')
        gate_refs = []
    for ref in gate_refs:
        try:
            receipt = _json_bytes(_artifact(root, ref, read=True))
            if not isinstance(receipt, dict):
                raise ValueError('gate receipt must be an object')
            gate = receipt.get('gate')
            if gate not in GATES or gate in receipts:
                raise ValueError('unknown or duplicate gate')
            receipts[gate] = receipt
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            errors.append('gate receipt: ' + str(exc))
    missing.extend(sorted(GATES - receipts.keys()))
    for gate, receipt in receipts.items():
        try:
            if receipt.get('candidate') != candidate or receipt.get('status') != 'PASS':
                raise ValueError('gate candidate differs or gate did not pass')
            refs = receipt['artifacts']
            artifacts = {}
            for ref in refs:
                _artifact(root, ref)
                if ref['path'] in artifacts:
                    raise ValueError('duplicate artifact path')
                artifacts[ref['path']] = ref
            commands = {}
            tests = {}
            for command in receipt['commands']:
                cid = command['id']
                if not isinstance(cid, str) or not cid or cid in command_ids:
                    raise ValueError('missing or reused command ID')
                command_ids.add(cid)
                if type(command['exit_code']) is not int or command['exit_code'] != 0:
                    raise ValueError('producer command failed')
                if not isinstance(command['argv'], list) or not command['argv'] or not all(isinstance(x, str) and x for x in command['argv']):
                    raise ValueError('command argv missing')
                if not command.get('cwd') or not isinstance(command.get('environment'), dict) or not command['environment']:
                    raise ValueError('command environment missing')
                if _time(command['finished_at']) < _time(command['started_at']):
                    raise ValueError('invalid command time ordering')
                for key in ('stdout', 'stderr', 'junit'):
                    path = command[key]
                    if path not in artifacts or path in log_paths:
                        raise ValueError('unindexed or reused command output')
                    log_paths.add(path)
                commands[cid] = command
                tests[cid] = _tests(_artifact(root, artifacts[command['junit']], read=True))
            checks = receipt['checks']
            check_ids = [check['id'] for check in checks]
            if len(check_ids) != len(set(check_ids)) or set(check_ids) != set(required[gate]):
                raise ValueError('required gate check set differs')
            used_test_ids = set()
            for check in checks:
                if check.get('status') != 'PASS' or check['command_id'] not in commands:
                    raise ValueError('gate check failed or lacks producer')
                ids = check['test_ids']
                if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or not set(ids) <= tests[check['command_id']]:
                    raise ValueError('check lacks exact passing test identities')
                if used_test_ids.intersection(ids):
                    raise ValueError('test identity reused for multiple gate checks')
                used_test_ids.update(ids)
                allowed = {'baseline_oracle', 'product_test', 'native_test', 'performance', 'independent_review'}
                if check.get('kind') not in allowed or (gate not in {'G0', 'H0', 'L0', 'A0'} and check['kind'] == 'baseline_oracle'):
                    raise ValueError('documentation or baseline validation cannot certify product behavior')
                if not selftest and check['kind'] != EXPECTED_CHECK_KINDS[check['id']]:
                    raise ValueError('gate check evidence kind differs from frozen contract')
            if not selftest and gate.startswith('G'):
                from tools.verify import validate_evidence
                if receipt['legacy_manifest'] not in artifacts:
                    raise ValueError('original G manifest is not an authenticated artifact')
                legacy = _path(root, receipt['legacy_manifest']).parent
                legacy_errors = validate_evidence(legacy)
                if legacy_errors:
                    raise ValueError('original G validator rejected evidence: ' + '; '.join(legacy_errors))
                manifest = _json_bytes(_artifact(root, artifacts[receipt['legacy_manifest']], read=True))
                if (manifest.get('gate_id') != gate or manifest.get('candidate', {}).get('commit') != candidate['commit']
                        or manifest.get('status') != 'PASS'):
                    raise ValueError('original G candidate mismatch')
            passed_checks.update(check_ids)
            passing_by_gate[gate] = {check['id']: check for check in checks}
            validated.append(gate)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ET.ParseError) as exc:
            errors.append(gate + ': ' + str(exc))
    try:
        requirements = registry['requirements']
        expected = {row['id'] for row in requirements if row['disposition'] == 'mandatory'}
        if len({row['id'] for row in requirements}) != len(requirements) or not expected:
            raise ValueError('invalid or empty requirement register')
        results = bundle['requirement_results']
        ids = [row['id'] for row in results]
        if len(ids) != len(set(ids)) or set(ids) != expected:
            raise ValueError('mandatory requirement accounting differs')
        for row in results:
            if (row.get('status') != 'PASS' or not isinstance(row.get('checks'), list)
                    or not row['checks'] or not set(row['checks']) <= passed_checks):
                raise ValueError('requirement lacks valid passing checks')
            if not selftest and (proof_map is None or len(row['checks']) != len(set(row['checks']))
                    or set(proof_map[row['id']]) != set(row['checks'])):
                raise ValueError('requirement proof checks differ from frozen semantic mapping')
        review = bundle['fresh_review']
        if (not isinstance(review, dict) or review.get('independent') is not True
                or _identity(review.get('reviewer')) is None
                or not isinstance(review.get('implementers'), list) or not review['implementers']
                or not all(_identity(name) for name in review['implementers'])
                or len({_identity(name) for name in review['implementers']}) != len(review['implementers'])
                or _identity(review['reviewer']) in {_identity(name) for name in review['implementers']}
                or not isinstance(review.get('areas'), list) or len(review['areas']) != len(REVIEW_AREAS)
                or set(review['areas']) != REVIEW_AREAS or review.get('gate') != 'A7' or 'A7' not in validated):
            raise ValueError('consequential fresh review incomplete or not independent')
        if not selftest and any(passing_by_gate['A7'][f'A7.{area}']['kind'] != 'independent_review'
                                for area in REVIEW_AREAS):
            raise ValueError('A7 review areas lack independent review checks')
    except (ValueError, KeyError, TypeError) as exc:
        errors.append('requirement/review: ' + str(exc))
    if not selftest and commit_time is not None:
        try:
            _verify_command_timing(receipts, validated, commit_time)
        except (OSError, ValueError, KeyError, TypeError, StopIteration, AttributeError,
                subprocess.CalledProcessError) as exc:
            errors.append('candidate command timing: ' + str(exc))
        try:
            if _product_inputs(root, bundle, candidate, registry) != commit_time:
                raise ValueError('candidate commit timestamp changed during validation')
        except (OSError, ValueError, KeyError, TypeError, AttributeError,
                subprocess.CalledProcessError) as exc:
            errors.append('candidate changed during validation: ' + str(exc))
    status = 'FAIL' if errors else 'INCOMPLETE' if missing else 'SELFTEST_PASS' if selftest else 'PASS'
    return {'schema': 'rwsi-aggregate-result/1', 'status': status, 'product_accepted': status == 'PASS',
        'candidate': candidate, 'validated_gates': sorted(validated), 'missing_gates': missing, 'errors': errors}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--registry', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_bundle(args.bundle.parent, _json(args.bundle), _json(args.candidate), _json(args.registry))
    except (OSError, ValueError, TypeError) as exc:
        result = {'status': 'FAIL', 'product_accepted': False, 'errors': [str(exc)]}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['product_accepted'] else 2 if result['status'] == 'INCOMPLETE' else 1


if __name__ == '__main__':
    sys.exit(main())
