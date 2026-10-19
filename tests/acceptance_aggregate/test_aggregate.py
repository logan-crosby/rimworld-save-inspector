"""Independent fault expectations for the evidence validator, not product acceptance."""
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock
import subprocess

from tools.aggregate_acceptance import verify_bundle, source_tree_fingerprint, _artifact
from tools.gates.complete_contract import REQUIRED_CASES, EXPECTED_CHECK_KINDS

GATES = {f'{family}{n}' for family in 'GHLA' for n in range(8)}
CANDIDATE = {'commit': 'a' * 40, 'source_tree_sha256': 'b' * 64,
             'wheel_sha256': 'c' * 64, 'sdist_sha256': 'd' * 64, 'recorder_sha256': 'e' * 64}


class AggregateFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = {'requirements': [{'id': 'required-1', 'disposition': 'mandatory'}]}
        self.contract = {gate: [gate + '.proof'] for gate in GATES}
        self.bundle = {'schema': 'rwsi-complete-acceptance/1', 'candidate': CANDIDATE,
            'purpose': 'validator_selftest', 'gates': [],
            'requirement_results': [{'id': 'required-1', 'status': 'PASS', 'checks': ['H1.proof']}],
            'fresh_review': {'reviewer': 'fresh', 'implementers': ['worker', 'root'], 'independent': True,
                'areas': ['installed', 'native', 'chronology', 'preservation', 'fault', 'discovery', 'performance'],
                'gate': 'A7'}}
        for gate in sorted(GATES):
            prefix = gate + '/'
            artifacts = [self.artifact(prefix + 'stdout.txt', b'ran independent proof\n'),
                self.artifact(prefix + 'stderr.txt', b''),
                self.artifact(prefix + 'junit.xml',
                    f'<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="proof" name="{gate}"/></testsuite>'.encode())]
            receipt = {'gate': gate, 'candidate': copy.deepcopy(CANDIDATE), 'status': 'PASS',
                'checks': [{'id': gate + '.proof', 'status': 'PASS', 'command_id': gate + '-cmd',
                    'test_ids': ['proof::' + gate], 'kind': 'native_test' if gate.startswith('L') else 'product_test'}],
                'commands': [{'id': gate + '-cmd', 'argv': ['python', 'independent-producer.py'],
                    'cwd': '/isolated', 'environment': {'python': '3.11'}, 'exit_code': 0,
                    'started_at': '2026-10-18T00:00:00Z', 'finished_at': '2026-10-18T00:00:01Z',
                    'stdout': artifacts[0]['path'], 'stderr': artifacts[1]['path'], 'junit': artifacts[2]['path']}],
                'artifacts': artifacts}
            path = prefix + 'receipt.json'
            self.write(path, receipt)
            self.bundle['gates'].append(self.ref(path))

    def write(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value))

    def ref(self, path):
        raw = (self.root / path).read_bytes()
        return {'path': path, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}

    def artifact(self, path, raw):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        return self.ref(path)

    def edit_receipt(self, gate, mutate):
        ref = next(x for x in self.bundle['gates'] if x['path'] == gate + '/receipt.json')
        receipt = json.loads((self.root / ref['path']).read_text())
        mutate(receipt)
        self.write(ref['path'], receipt)
        ref.update(self.ref(ref['path']))

    def run_validator(self):
        return verify_bundle(self.root, self.bundle, CANDIDATE, self.registry, self.contract)

    def test_complete_synthetic_bundle_can_only_pass_validator_selftest(self):
        result = self.run_validator()
        self.assertEqual(result['status'], 'SELFTEST_PASS', result)
        self.assertFalse(result['product_accepted'])
        self.assertEqual(set(result['validated_gates']), GATES)

    def test_missing_native_receipt_is_incomplete(self):
        self.bundle['gates'] = [x for x in self.bundle['gates'] if x['path'] != 'L5/receipt.json']
        self.assertEqual(self.run_validator()['status'], 'INCOMPLETE')

    def test_wrong_candidate_and_package_hash_fail(self):
        for field in ['commit', 'wheel_sha256', 'recorder_sha256']:
            with self.subTest(field=field):
                self.edit_receipt('H6', lambda r: r['candidate'].update({field: 'f' * len(CANDIDATE[field])}))
                self.assertEqual(self.run_validator()['status'], 'FAIL')
                self.edit_receipt('H6', lambda r: r.update(candidate=copy.deepcopy(CANDIDATE)))

    def test_skipped_failed_zero_and_duplicate_tests_fail_even_with_pass_receipt(self):
        variants = [b'<testsuite tests="1" skipped="1"><testcase classname="proof" name="H1"><skipped/></testcase></testsuite>',
            b'<testsuite tests="1"><testcase classname="proof" name="H1"><failure/></testcase></testsuite>',
            b'<testsuite tests="0"/>',
            b'<testsuite tests="2"><testcase classname="proof" name="H1"/><testcase classname="proof" name="H1"/></testsuite>']
        for raw in variants:
            with self.subTest(raw=raw):
                artifact = self.artifact('H1/junit.xml', raw)
                self.edit_receipt('H1', lambda r: r['artifacts'].__setitem__(2, artifact))
                self.assertEqual(self.run_validator()['status'], 'FAIL')

    def test_modified_log_and_receipt_hashes_fail(self):
        (self.root / 'L6/stdout.txt').write_text('altered')
        self.assertEqual(self.run_validator()['status'], 'FAIL')
        self.bundle['gates'][0]['sha256'] = '0' * 64
        self.assertEqual(self.run_validator()['status'], 'FAIL')

    def test_unindexed_logs_and_failed_commands_fail(self):
        self.edit_receipt('A6', lambda r: r['commands'][0].update(stdout='unindexed.txt', exit_code=1))
        self.assertEqual(self.run_validator()['status'], 'FAIL')

    def test_missing_required_check_and_requirement_fail(self):
        self.edit_receipt('A1', lambda r: r.update(checks=[]))
        self.assertEqual(self.run_validator()['status'], 'FAIL')
        self.bundle['requirement_results'] = []
        self.assertEqual(self.run_validator()['status'], 'FAIL')

    def test_fresh_review_cannot_be_implemented_by_reviewer_or_omit_area(self):
        self.bundle['fresh_review']['reviewer'] = 'root'
        self.assertEqual(self.run_validator()['status'], 'FAIL')
        self.bundle['fresh_review']['reviewer'] = 'fresh'
        self.bundle['fresh_review']['areas'].remove('native')
        self.assertEqual(self.run_validator()['status'], 'FAIL')

    def test_documentation_only_checks_cannot_certify_product_gate(self):
        self.edit_receipt('L5', lambda r: r['checks'][0].update(kind='baseline_oracle'))
        self.assertEqual(self.run_validator()['status'], 'FAIL')

    def test_duplicate_gate_and_path_escape_fail(self):
        self.bundle['gates'].append(self.bundle['gates'][0])
        self.assertEqual(self.run_validator()['status'], 'FAIL')
        self.bundle['gates'][-1] = {'path': '../outside.json', 'bytes': 1, 'sha256': '0' * 64}
        self.assertEqual(self.run_validator()['status'], 'FAIL')

    def test_selftest_cannot_be_relabelled_product_acceptance(self):
        self.bundle['purpose'] = 'product_acceptance'
        self.assertEqual(self.run_validator()['status'], 'FAIL')  # Missing authentic legacy G evidence.

    def test_malformed_xml_and_nonobject_inputs_fail_closed(self):
        artifact = self.artifact('H1/junit.xml', b'<testsuite><testcase')
        self.edit_receipt('H1', lambda r: r['artifacts'].__setitem__(2, artifact))
        self.assertEqual(self.run_validator()['status'], 'FAIL')
        self.assertEqual(verify_bundle(self.root, [], CANDIDATE, self.registry, self.contract)['status'], 'FAIL')

    def test_truthy_string_is_not_independent_review(self):
        self.bundle['fresh_review']['independent'] = 'false'
        self.assertEqual(self.run_validator()['status'], 'FAIL')

    def test_review_identity_must_be_strings_not_type_confused(self):
        self.bundle['fresh_review']['reviewer'] = {'id': 'fresh'}
        self.assertEqual(self.run_validator()['status'], 'FAIL')

    def test_receipt_parse_uses_only_authenticated_bytes(self):
        self.edit_receipt('H1', lambda r: r.update(status='FAIL'))
        target = self.root / 'H1/receipt.json'
        forged = json.loads(target.read_text())
        forged['status'] = 'PASS'
        original = Path.read_text

        def changed_second_read(path, *args, **kwargs):
            if path.resolve() == target.resolve():
                return json.dumps(forged)
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', changed_second_read):
            self.assertEqual(self.run_validator()['status'], 'FAIL')

    def test_junit_parse_uses_only_authenticated_bytes(self):
        target = self.root / 'H1/junit.xml'
        bad = b'<testsuite tests="1"><testcase classname="proof" name="H1"><failure/></testcase></testsuite>'
        ref = self.artifact('H1/junit.xml', bad)
        self.edit_receipt('H1', lambda r: r['artifacts'].__setitem__(2, ref))
        good = b'<testsuite tests="1"><testcase classname="proof" name="H1"/></testsuite>'
        original = Path.read_bytes

        def changed_second_read(path):
            return good if path.resolve() == target.resolve() else original(path)

        with mock.patch.object(Path, 'read_bytes', changed_second_read):
            self.assertEqual(self.run_validator()['status'], 'FAIL')

    def test_malformed_gate_population_fails_closed(self):
        for gates in (None, 42, {'receipt': 'H1'}, 'H1'):
            with self.subTest(gates=gates):
                self.bundle['gates'] = gates
                self.assertEqual(self.run_validator()['status'], 'FAIL')

    def test_nested_junit_declared_count_cannot_hide_case(self):
        raw = b'<testsuites tests="0"><testsuite tests="0"><testcase classname="proof" name="H1"/></testsuite></testsuites>'
        ref = self.artifact('H1/junit.xml', raw)
        self.edit_receipt('H1', lambda r: r['artifacts'].__setitem__(2, ref))
        self.assertEqual(self.run_validator()['status'], 'FAIL')

    def prepare_product_oracle(self):
        """Disposable product-path fixture; original G semantics are independently stubbed."""
        source = self.root / 'candidate-source'
        source.mkdir()
        (source / 'candidate.txt').write_text('final candidate bytes\n')
        (source / 'recorder').mkdir()
        (source / 'recorder/native.txt').write_text('unchanged native code\n')
        (source / 'tests/native_acceptance').mkdir(parents=True)
        (source / 'tests/native_acceptance/harness.txt').write_text('unchanged native harness\n')
        subprocess.run(['git', 'init', '-q', str(source)], check=True)
        subprocess.run(['git', 'add', 'candidate.txt', 'recorder/native.txt',
                        'tests/native_acceptance/harness.txt'], cwd=source, check=True)
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '-qm', 'fixture'], cwd=source, check=True)
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
        after_commit = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2)).isoformat().replace('+00:00', 'Z')
        registry = json.loads((Path(__file__).resolve().parents[2] / 'docs/requirements/requirement-register.json').read_text())
        candidate = {'commit': commit, 'source_tree_sha256': source_tree_fingerprint(source),
                     **{name + '_sha256': hashlib.sha256(name.encode()).hexdigest()
                        for name in ('wheel', 'sdist', 'recorder')}}
        self.bundle['candidate'] = candidate
        self.bundle['purpose'] = 'product_acceptance'
        self.bundle['packages'] = {name: self.artifact('packages/' + name, name.encode())
                                   for name in ('wheel', 'sdist', 'recorder')}
        self.bundle['requirement_results'] = [
            {'id': row['id'], 'status': 'PASS', 'checks': self.oracle_requirement_checks(row)}
            for row in registry['requirements'] if row['disposition'] == 'mandatory']
        for gate in sorted(GATES):
            ids = REQUIRED_CASES[gate]
            junit = '<testsuite tests="{}">{}</testsuite>'.format(
                len(ids), ''.join(f'<testcase classname="proof" name="{name}"/>' for name in ids))
            junit_ref = self.artifact(gate + '/junit.xml', junit.encode())
            legacy = {'gate_id': gate, 'candidate': {'commit': commit}, 'status': 'PASS'}
            self.write(gate + '/legacy/manifest.json', legacy)
            legacy_ref = self.ref(gate + '/legacy/manifest.json')

            def update(r):
                r['candidate'] = candidate
                r['checks'] = [{'id': name, 'status': 'PASS', 'command_id': gate + '-cmd',
                                'test_ids': ['proof::' + name],
                                'kind': EXPECTED_CHECK_KINDS[name]} for name in ids]
                r['artifacts'][2] = junit_ref
                r['commands'][0]['started_at'] = after_commit
                r['commands'][0]['finished_at'] = after_commit
                if gate.startswith('G'):
                    r['legacy_manifest'] = gate + '/legacy/manifest.json'
                    r['artifacts'].append(legacy_ref)
            self.edit_receipt(gate, update)
        return source, candidate, registry

    @staticmethod
    def oracle_requirement_checks(row):
        mapping = json.loads((Path(__file__).resolve().parents[2] / 'docs/requirements/acceptance-proof-map.json').read_text())
        return mapping['requirements'][row['id']]

    @staticmethod
    def first_check_of_each_named_gate(row):
        import re
        named = set(re.findall(r'\b[GHLA][0-7]\b', row['acceptance_test']))
        if row['id'] == 'L-12':
            named.update(('L4', 'L5', 'L6', 'L7'))
        if not named:
            named = {'A5'} if row['id'].startswith(('DISC-', 'REC-')) else {'A7'}
        return [REQUIRED_CASES[gate][0] for gate in sorted(named)]

    def test_product_path_authenticates_real_git_and_package_bytes(self):
        source, candidate, registry = self.prepare_product_oracle()
        with mock.patch('tools.aggregate_acceptance.SOURCE_ROOT', source), \
             mock.patch('tools.verify.validate_evidence', return_value=[]):
            result = verify_bundle(self.root, self.bundle, candidate, registry)
            self.assertEqual(result['status'], 'PASS', result)
            (self.root / 'packages/wheel').write_bytes(b'tampered')
            self.assertEqual(verify_bundle(self.root, self.bundle, candidate, registry)['status'], 'FAIL')
            (self.root / 'packages/wheel').write_bytes(b'wheel')
            (source / 'candidate.txt').write_text('changed source\n')
            self.assertEqual(verify_bundle(self.root, self.bundle, candidate, registry)['status'], 'FAIL')

    def test_product_path_rejects_unindexed_legacy_manifest(self):
        source, candidate, registry = self.prepare_product_oracle()
        self.edit_receipt('G1', lambda r: r['artifacts'].pop())
        with mock.patch('tools.aggregate_acceptance.SOURCE_ROOT', source), \
             mock.patch('tools.verify.validate_evidence', return_value=[]):
            self.assertEqual(verify_bundle(self.root, self.bundle, candidate, registry)['status'], 'FAIL')

    def test_product_path_rejects_unrelated_requirement_proof(self):
        source, candidate, registry = self.prepare_product_oracle()
        self.bundle['requirement_results'][0]['checks'] = ['A7.aggregate_fault_controls']
        with mock.patch('tools.aggregate_acceptance.SOURCE_ROOT', source), \
             mock.patch('tools.verify.validate_evidence', return_value=[]):
            self.assertEqual(verify_bundle(self.root, self.bundle, candidate, registry)['status'], 'FAIL')

    def test_product_path_requires_independent_a7_check(self):
        source, candidate, registry = self.prepare_product_oracle()
        self.edit_receipt('A7', lambda r: [c.update(kind='product_test') for c in r['checks']])
        with mock.patch('tools.aggregate_acceptance.SOURCE_ROOT', source), \
             mock.patch('tools.verify.validate_evidence', return_value=[]):
            self.assertEqual(verify_bundle(self.root, self.bundle, candidate, registry)['status'], 'FAIL')

    def test_product_path_requires_native_and_performance_check_kinds(self):
        source, candidate, registry = self.prepare_product_oracle()
        for gate, check_id in [('L5', 'L5.all_families'), ('L6', 'L6.tick_overhead'),
                               ('H6', 'H6.per_pawn_warm_p95'), ('A7', 'A7.requirement_coverage')]:
            with self.subTest(gate=gate, check_id=check_id):
                self.edit_receipt(gate, lambda r: next(c for c in r['checks'] if c['id'] == check_id).update(kind='product_test'))
                with mock.patch('tools.aggregate_acceptance.SOURCE_ROOT', source), \
                     mock.patch('tools.verify.validate_evidence', return_value=[]):
                    self.assertEqual(verify_bundle(self.root, self.bundle, candidate, registry)['status'], 'FAIL')
                self.edit_receipt(gate, lambda r: next(c for c in r['checks'] if c['id'] == check_id).update(kind=EXPECTED_CHECK_KINDS[check_id]))

    def test_product_path_rejects_1970_producer_command(self):
        source, candidate, registry = self.prepare_product_oracle()
        self.edit_receipt('H1', lambda r: r['commands'][0].update(
            started_at='1970-01-01T00:00:00Z', finished_at='1970-01-01T00:00:01Z'))
        with mock.patch('tools.aggregate_acceptance.SOURCE_ROOT', source), \
             mock.patch('tools.verify.validate_evidence', return_value=[]):
            self.assertEqual(verify_bundle(self.root, self.bundle, candidate, registry)['status'], 'FAIL')

    def test_product_path_rejects_future_dated_build_and_commands(self):
        source, candidate, registry = self.prepare_product_oracle()
        for gate in sorted(GATES):
            self.edit_receipt(gate, lambda r: r['commands'][0].update(
                started_at='2099-01-01T00:00:00Z', finished_at='2099-01-01T00:00:00Z'))
        with mock.patch('tools.aggregate_acceptance.SOURCE_ROOT', source), \
             mock.patch('tools.verify.validate_evidence', return_value=[]):
            self.assertEqual(verify_bundle(self.root, self.bundle, candidate, registry)['status'], 'FAIL')

    def test_product_path_rejects_one_future_dated_producer(self):
        source, candidate, registry = self.prepare_product_oracle()
        for gate in ('H0', 'H1', 'A7'):
            with self.subTest(gate=gate):
                original = json.loads((self.root / gate / 'receipt.json').read_text())['commands'][0]
                self.edit_receipt(gate, lambda r: r['commands'][0].update(
                    started_at='2099-01-01T00:00:00Z', finished_at='2099-01-01T00:00:01Z'))
                with mock.patch('tools.aggregate_acceptance.SOURCE_ROOT', source), \
                     mock.patch('tools.verify.validate_evidence', return_value=[]):
                    self.assertEqual(verify_bundle(self.root, self.bundle, candidate, registry)['status'], 'FAIL')
                self.edit_receipt(gate, lambda r: r['commands'][0].update(
                    started_at=original['started_at'], finished_at=original['finished_at']))

    def test_old_native_command_cannot_be_reused_with_declared_approval(self):
        source, candidate, registry = self.prepare_product_oracle()
        prior = dict(candidate, commit='f' * 40, source_tree_sha256='f' * 64,
                     wheel_sha256='f' * 64, sdist_sha256='f' * 64)
        names = ('recorder/native.txt', 'tests/native_acceptance/harness.txt')
        rows = [[name, (source / name).stat().st_size,
                 hashlib.sha256((source / name).read_bytes()).hexdigest()] for name in names]
        closure = hashlib.sha256(json.dumps(rows, ensure_ascii=False,
                                            separators=(',', ':')).encode()).hexdigest()
        reuse = {'basis': 'unchanged_recorder', 'prior_candidate': prior,
                 'dependency_fingerprint': closure, 'approval_artifact': 'A7/native-reuse.json',
                 'reason': 'Native recorder source and harness closure is byte-identical.'}
        approval_time = json.loads((self.root / 'A7/receipt.json').read_text())['commands'][0]['finished_at']
        approval = {'schema': 'rwsi-reuse-approval/1', 'approved': True,
                    'gate': 'L5', 'command_id': 'L5-cmd', 'final_candidate': candidate,
                    'prior_candidate': prior, 'dependency_fingerprint': closure,
                    'reason': reuse['reason'], 'reviewer': 'fresh',
                    'approved_at': approval_time}
        ref = self.artifact('A7/native-reuse.json', json.dumps(approval).encode())
        self.edit_receipt('A7', lambda r: r['artifacts'].append(ref))
        self.edit_receipt('L5', lambda r: r['commands'][0].update(
            started_at='1970-01-01T00:00:00Z', finished_at='1970-01-01T00:00:01Z', reuse=reuse))
        with mock.patch('tools.aggregate_acceptance.SOURCE_ROOT', source), \
             mock.patch('tools.verify.validate_evidence', return_value=[]):
            result = verify_bundle(self.root, self.bundle, candidate, registry)
            self.assertEqual(result['status'], 'FAIL', result)
            self.assertFalse(result['product_accepted'])
            evidence = os.environ.get('RWSI_AGGREGATE_SELFTEST_EVIDENCE')
            if evidence:
                destination = Path(evidence) / 'invalid_native_reuse'
                shutil.copytree(self.root, destination)
                for name, value in {'bundle': self.bundle, 'external-candidate': candidate,
                                    'registry': registry, 'result': result}.items():
                    (destination / (name + '.json')).write_text(json.dumps(value, indent=2))

    def test_product_path_requires_specific_discovery_proofs(self):
        source, candidate, registry = self.prepare_product_oracle()
        expected = {'DISC-03': 'A5.source_channels', 'DISC-09': 'A5.evidence_use_ledger',
                    'DISC-11': 'A5.historical_artifact_links', 'DISC-12': 'A5.bounded_followups',
                    'DISC-13': 'A6.cache_invalidation', 'REC-P5': 'A5.preinstallation_abstention'}
        for requirement_id, check_id in expected.items():
            with self.subTest(requirement_id=requirement_id):
                row = next(row for row in self.bundle['requirement_results'] if row['id'] == requirement_id)
                self.assertIn(check_id, row['checks'])
                row['checks'].remove(check_id)
                with mock.patch('tools.aggregate_acceptance.SOURCE_ROOT', source), \
                     mock.patch('tools.verify.validate_evidence', return_value=[]):
                    self.assertEqual(verify_bundle(self.root, self.bundle, candidate, registry)['status'], 'FAIL')
                row['checks'].append(check_id)

    def test_product_path_rejects_first_check_of_each_gate_as_requirement_proof(self):
        source, candidate, registry = self.prepare_product_oracle()
        self.bundle['requirement_results'] = [
            {'id': row['id'], 'status': 'PASS', 'checks': self.first_check_of_each_named_gate(row)}
            for row in registry['requirements'] if row['disposition'] == 'mandatory']
        with mock.patch('tools.aggregate_acceptance.SOURCE_ROOT', source), \
             mock.patch('tools.verify.validate_evidence', return_value=[]):
            self.assertEqual(verify_bundle(self.root, self.bundle, candidate, registry)['status'], 'FAIL')

    def test_product_path_rejects_extra_unmapped_requirement_check(self):
        source, candidate, registry = self.prepare_product_oracle()
        self.bundle['requirement_results'][0]['checks'].append('A7.aggregate_fault_controls')
        with mock.patch('tools.aggregate_acceptance.SOURCE_ROOT', source), \
             mock.patch('tools.verify.validate_evidence', return_value=[]):
            self.assertEqual(verify_bundle(self.root, self.bundle, candidate, registry)['status'], 'FAIL')

    def test_reviewer_identity_normalization_rejects_alias(self):
        self.bundle['fresh_review']['reviewer'] = ' Root '
        self.assertEqual(self.run_validator()['status'], 'FAIL')

    def test_parent_directory_symlink_swap_cannot_authenticate_outside_bytes(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        inside = self.root / 'swappable'
        inside.mkdir()
        (inside / 'proof.txt').write_bytes(b'inside')
        (Path(outside.name) / 'proof.txt').write_bytes(b'outside')
        raw = b'outside'
        ref = {'path': 'swappable/proof.txt', 'bytes': len(raw),
               'sha256': hashlib.sha256(raw).hexdigest()}
        from tools import aggregate_acceptance as module
        original = module._path
        def swap_after_check(root, value):
            path = original(root, value)
            inside.rename(self.root / 'swappable-old')
            inside.symlink_to(Path(outside.name), target_is_directory=True)
            return path
        with mock.patch.object(module, '_path', swap_after_check):
            with self.assertRaises((OSError, ValueError)):
                _artifact(self.root, ref)

    def test_product_path_rejects_other_gate_legacy_manifest(self):
        source, candidate, registry = self.prepare_product_oracle()
        manifest = self.root / 'G1/legacy/manifest.json'
        value = json.loads(manifest.read_text())
        value['gate_id'] = 'G2'
        self.write('G1/legacy/manifest.json', value)
        self.edit_receipt('G1', lambda r: r['artifacts'][-1].update(self.ref('G1/legacy/manifest.json')))
        with mock.patch('tools.aggregate_acceptance.SOURCE_ROOT', source), \
             mock.patch('tools.verify.validate_evidence', return_value=[]):
            self.assertEqual(verify_bundle(self.root, self.bundle, candidate, registry)['status'], 'FAIL')

    def test_one_test_identity_cannot_certify_two_checks(self):
        self.contract['H1'] = ['H1.first', 'H1.second']
        self.bundle['requirement_results'][0]['checks'] = ['H1.first']
        self.edit_receipt('H1', lambda r: r['checks'].__setitem__(slice(None), [
            {'id': check_id, 'status': 'PASS', 'command_id': 'H1-cmd',
             'test_ids': ['proof::H1'], 'kind': 'product_test'} for check_id in self.contract['H1']]))
        self.assertEqual(self.run_validator()['status'], 'FAIL')

    def test_synthetic_runs_retain_missing_wrong_candidate_failed_and_restored_evidence(self):
        # This is an executable validator fault demonstration, never native success.
        saved = copy.deepcopy(self.bundle)
        outcomes = {}
        evidence = os.environ.get('RWSI_AGGREGATE_SELFTEST_EVIDENCE')
        def retain(label):
            if evidence:
                destination = Path(evidence) / label
                shutil.copytree(self.root, destination)
                for name, value in {'bundle': self.bundle, 'external-candidate': CANDIDATE,
                    'registry': self.registry, 'selftest-contract': self.contract, 'result': outcomes[label]}.items():
                    (destination / (name + '.json')).write_text(json.dumps(value, indent=2))
        self.edit_receipt('A5', lambda r: r['checks'][0].update(status='FAIL'))
        outcomes['failed_check'] = self.run_validator()
        retain('failed_check')
        self.edit_receipt('A5', lambda r: r['checks'][0].update(status='PASS'))
        self.bundle['gates'] = [x for x in self.bundle['gates'] if x['path'] != 'L2/receipt.json']
        outcomes['missing_native'] = self.run_validator()
        retain('missing_native')
        self.bundle = saved
        self.bundle['candidate'] = dict(CANDIDATE, wheel_sha256='f' * 64)
        outcomes['wrong_candidate'] = self.run_validator()
        retain('wrong_candidate')
        self.bundle['candidate'] = CANDIDATE
        outcomes['restored'] = self.run_validator()
        retain('restored')
        self.assertEqual({k: v['status'] for k, v in outcomes.items()},
            {'failed_check': 'FAIL', 'missing_native': 'INCOMPLETE', 'wrong_candidate': 'FAIL', 'restored': 'SELFTEST_PASS'})
        self.assertTrue(all(not x['product_accepted'] for x in outcomes.values()))


if __name__ == '__main__':
    unittest.main()
