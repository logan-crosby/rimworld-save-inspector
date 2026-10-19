"""Mandatory evidence sections derived from the frozen G/H/L/A contracts.

Section coverage is necessary, not sufficient: real assertion producers and a
fresh reviewer verify their semantics. Original G evidence validators remain in force.
"""
from tools.gates.cases import GATE_CASES

SECTIONS = {
    'G0': ['independent_baseline'],
    **{gate: list(cases) for gate, cases in GATE_CASES.items()},
    'H0': ['synthetic_gold', 'private_manifest', 'cohort', 'volume'],
    'H1': ['scopes', 'domains', 'unknowns', 'limits', 'mutation_cancellation', 'preservation'],
    'H2': ['identity', 'duplicates_conflicts', 'failed_sources', 'atomic_refresh', 'collision'],
    'H3': ['clocks', 'events', 'aliases_references', 'missing_data', 'branches', 'evidence'],
    'H4': ['equivalence', 'one_call', 'schemas', 'freshness', 'concurrency_cancellation', 'selectors_cursors', 'caps_boundaries', 'legacy'],
    'H5': ['all_sources', 'exact_cohort', 'context_cases', 'all_one_call_sizes', 'preservation'],
    'H6': ['wheel_sdist', 'outside_install', 'public_regression', 'privacy_matrix', 'cold_refresh_strict_costs', 'per_pawn_warm_p95', 'memory'],
    'H7': ['installed', 'chronology', 'preservation', 'one_call', 'privacy', 'final_candidate'],
    'L0': ['independent_journals', 'negatives', 'hook_audit', 'environment'],
    'L1': ['queue_ownership', 'sequences_gaps', 'rotation_caps', 'failures_shutdown', 'real_serializer'],
    'L2': ['reference_builds', 'actual_native_hooks', 'success_predicates', 'clocks_baselines'],
    'L3': ['independent_cross_language', 'envelope', 'conflicts_unknowns', 'unsafe_mutation', 'limits_global_coverage', 'no_inferred_lineage'],
    'L4': ['installed_equivalence', 'one_call_errors_schema', 'legacy', 'distribution_privacy'],
    'L5': ['synthetic_screenshots', 'lifecycle_replays', 'all_families', 'all_holders', 'install_remove_reinstall', 'failed_saves_checkpoint_hash', 'preservation'],
    'L6': ['three_matched_pairs', 'tick_overhead', 'hook_p95_samples', 'queue_memory', 'separate_baseline_flush', 'fault_overflow', 'stability'],
    'L7': ['installed', 'native', 'branch_clock', 'faults', 'final_candidate'],
    'A0': ['reconciled_scope', 'independent_gold', 'unseen_holdout', 'fixed_thresholds', 'preserved_failures'],
    'A1': ['complete_extraction', 'exact_population', 'presence_states', 'lossless_context'],
    'A2': ['identity_clocks', 'branches_conflicts', 'historical_context', 'absence_not_events', 'association_not_cause'],
    'A3': ['native_predicates', 'checkpoint_watermarks', 'lifecycle_gaps', 'storage_failures_recovery', 'shutdown'],
    'A4': ['outside_equivalence', 'one_call', 'boundaries_cancellation', 'concurrent_stale_malformed', 'legacy', 'actual_serializer'],
    'A5': ['fresh_blind_evaluator', 'recall_precision', 'citation_support', 'abstention', 'calls_bytes_time', 'context_perspectives', 'all_case_outcomes', 'source_channels', 'evidence_use_ledger', 'historical_artifact_links', 'bounded_followups', 'preinstallation_abstention'],
    'A6': ['original_limits', 'boundary_interactions', 'offline_adapter', 'deterministic_baseline_no_pruning', 'fixed_thresholds', 'cache_invalidation'],
    'A7': ['installed', 'native', 'chronology', 'preservation', 'fault', 'discovery', 'performance', 'requirement_coverage', 'aggregate_fault_controls'],
}
REQUIRED_CASES = {gate: [f'{gate}.{name}' for name in names] for gate, names in SECTIONS.items()}
REVIEW_AREAS = {'installed', 'native', 'chronology', 'preservation', 'fault', 'discovery', 'performance'}
FROZEN_REGISTRY_CANONICAL_SHA256 = '7903c1ee1292092ebd9b033107a0ca932d23cbbb756cefad5ff92de2a4ce369b'
FROZEN_PROOF_MAP_CANONICAL_SHA256 = '9b954787ca0946f3d1c6fa75b6b0a641a142a5a48b7502df5767caed0d5941f5'

# The evidence kind is part of the frozen check identity, not a producer choice.
EXPECTED_CHECK_KINDS = {check: 'product_test' for checks in REQUIRED_CASES.values() for check in checks}
for gate in ('G0', 'H0', 'A0'):
    EXPECTED_CHECK_KINDS.update({check: 'baseline_oracle' for check in REQUIRED_CASES[gate]})
for name in ('independent_journals', 'negatives', 'environment'):
    EXPECTED_CHECK_KINDS['L0.' + name] = 'baseline_oracle'
for name in ('T36', 'T37'):
    EXPECTED_CHECK_KINDS['G7.' + name] = 'independent_review'
for gate in ('H7', 'L7', 'A7'):
    EXPECTED_CHECK_KINDS.update({check: 'independent_review' for check in REQUIRED_CASES[gate]})
for name in ('actual_native_hooks', 'success_predicates', 'clocks_baselines'):
    EXPECTED_CHECK_KINDS['L2.' + name] = 'native_test'
EXPECTED_CHECK_KINDS.update({check: 'native_test' for check in REQUIRED_CASES['L5']})
EXPECTED_CHECK_KINDS['L6.fault_overflow'] = 'native_test'
for name in ('three_matched_pairs', 'tick_overhead', 'hook_p95_samples', 'queue_memory',
             'separate_baseline_flush', 'stability'):
    EXPECTED_CHECK_KINDS['L6.' + name] = 'performance'
for name in ('cold_refresh_strict_costs', 'per_pawn_warm_p95', 'memory'):
    EXPECTED_CHECK_KINDS['H6.' + name] = 'performance'
EXPECTED_CHECK_KINDS['G6.T35'] = 'performance'
EXPECTED_CHECK_KINDS.update({check: 'native_test' for check in REQUIRED_CASES['A3']})
for name in ('original_limits', 'boundary_interactions', 'deterministic_baseline_no_pruning'):
    EXPECTED_CHECK_KINDS['A6.' + name] = 'performance'
