"""Offline tests for the exact anonymization optimizer and independent verify.

Covers the five acceptance groups:
1. the two pre-approved contracts produce different published tables/losses;
2. exact optimality, including anti-greedy and brute-force cross-checks;
3. whole-table equivalence-class merging and absence of internal ids;
4. bad hierarchies, infeasibility, missing sensitive values, resource bounds;
5. version binding, idempotency, tamper detection and independent verification.
"""

import copy
import json
import random
from itertools import product
from pathlib import Path

import pytest

from purposegate import anonymize
from purposegate.anon_optimize import (
    HierarchyError,
    build_publication,
    common_ancestor,
    evaluate_class,
    solve,
    validate_document,
    validate_hierarchy,
)
from purposegate.ingest import initialize
from purposegate.store import connect

FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return path


def load_fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf-8'))


def setup_source(tmp_path, config='anon-config.json', source='anon-source.json'):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / config)
    assert anonymize.record(db, FIX / source) == load_fixture(source)['source_id']
    return db


# ---------------------------------------------------------------- group 1: the two contracts

def test_two_contracts_produce_different_published_tables_and_losses(tmp_path):
    db = setup_source(tmp_path)
    l1 = anonymize.optimize(db, FIX / 'anon-opt-l1.json')
    l2 = anonymize.optimize(db, FIX / 'anon-opt-l2.json')

    assert l1['status'] == l2['status'] == 'optimal'
    assert l1['loss'] == 6 and l2['loss'] == 14
    assert l1['internal_groups'] == [['r1', 'r2'], ['r3', 'r4'], ['r5', 'r6']]
    assert l2['internal_groups'] == [['r1', 'r3'], ['r2', 'r4'], ['r5', 'r6']]

    qi1 = [(tuple(eq['qi'].values()), eq['size'], eq['sensitive_distinct'])
           for eq in l1['equivalence_classes']]
    assert qi1 == [(('20s', '100'), 2, 1), (('20s', '101'), 2, 1),
                   (('30s', '200'), 2, 2)]
    qi2 = [(tuple(eq['qi'].values()), eq['size'], eq['sensitive_distinct'])
           for eq in l2['equivalence_classes']]
    assert qi2 == [(('20', '10*'), 2, 2), (('21', '10*'), 2, 2),
                   (('30s', '200'), 2, 2)]

    # l=1 must not overpay for diversity: its age/zip values stay cheaper.
    assert l1['loss_breakdown'] == [
        {'eq_index': 0, 'loss': 2, 'column_loss': {'age': 2, 'zip': 0}},
        {'eq_index': 1, 'loss': 2, 'column_loss': {'age': 2, 'zip': 0}},
        {'eq_index': 2, 'loss': 2, 'column_loss': {'age': 2, 'zip': 0}},
    ]
    assert l2['loss_breakdown'] == [
        {'eq_index': 0, 'loss': 6, 'column_loss': {'age': 0, 'zip': 6}},
        {'eq_index': 1, 'loss': 6, 'column_loss': {'age': 0, 'zip': 6}},
        {'eq_index': 2, 'loss': 2, 'column_loss': {'age': 2, 'zip': 0}},
    ]

    # Published age/zip/sensitive values are queryable as real rows, not labels.
    rows1 = {(r['age'], r['zip'], r['condition']) for r in l1['published_rows']}
    assert rows1 == {('20s', '100', 'A'), ('20s', '101', 'B'),
                     ('30s', '200', 'A'), ('30s', '200', 'B')}
    rows2 = {(r['age'], r['zip'], r['condition']) for r in l2['published_rows']}
    assert rows2 == {('20', '10*', 'A'), ('20', '10*', 'B'),
                     ('21', '10*', 'A'), ('21', '10*', 'B'),
                     ('30s', '200', 'A'), ('30s', '200', 'B')}

    # Same-ZIP all-A / all-B small groups must not be misreported as diverse.
    by_tuple_l1 = {tuple(eq['qi'].values()): eq for eq in l1['equivalence_classes']}
    assert by_tuple_l1[('20s', '100')]['sensitive_values'] == ['A']
    assert by_tuple_l1[('20s', '101')]['sensitive_values'] == ['B']


def test_persisted_row_audit_maps_every_internal_id_without_publishing_it(tmp_path):
    db = setup_source(tmp_path)
    result = anonymize.optimize(db, FIX / 'anon-opt-l2.json')
    assert [a['row_id'] for a in result['row_audit']] == ['r1', 'r2', 'r3', 'r4', 'r5', 'r6']
    assert all('row_id' not in row for row in result['published_rows'])
    stored = connect(db).execute(
        'SELECT count(*) FROM anon_opt_row WHERE plan_id=?', ('anon-opt-l2',)).fetchone()[0]
    assert stored == 6
    # The published table itself carries no direct internal identifier column.
    assert all(set(row) == {'eq_index', 'age', 'zip', 'condition'}
               for row in result['published_rows'])


# ------------------------------------------------ group 2: exact optimality, anti-greedy

def _all_set_partitions(indices):
    """Exhaustive Bell-number enumeration of all set partitions."""
    if not indices:
        yield ()
        return
    first, rest = indices[0], indices[1:]
    for smaller in _all_set_partitions(rest):
        for pos in range(len(smaller) + 1):
            block = list(smaller[pos]) if pos < len(smaller) else []
            new_blocks = list(smaller)
            if pos < len(smaller):
                new_blocks[pos] = tuple(sorted(block + [first]))
            else:
                new_blocks.append((first,))
            yield tuple(sorted(new_blocks))


def _brute_force_optimum(doc, k, l_distinct):
    best = None
    n = len(doc['rows'])
    for partition in _all_set_partitions(list(range(n))):
        total, feasible = 0, True
        for members in partition:
            if len(members) < k:
                feasible = False
                break
            candidate = evaluate_class(members, doc)
            if candidate is None or candidate['sensitive_distinct'] < l_distinct:
                feasible = False
                break
            total += candidate['cost']
        if feasible and (best is None or total < best):
            best = total
    return best


def test_subset_dp_matches_exhaustive_brute_force_on_random_small_tables():
    rng = random.Random(20261118)
    age_tree = {'ancestors': {
        '20': ['20', '20s', '*'], '21': ['21', '20s', '*'],
        '30': ['30', '30s', '*'], '31': ['31', '30s', '*']},
        'costs': {'20': 0, '21': 0, '30': 0, '31': 0, '20s': 1, '30s': 1, '*': 10}}
    zip_tree = {'ancestors': {
        '100': ['100', '10*', '*'], '101': ['101', '10*', '*'],
        '200': ['200', '20*', '*'], '201': ['201', '20*', '*']},
        'costs': {'100': 0, '101': 0, '200': 0, '201': 0, '10*': 3, '20*': 3, '*': 10}}
    for case in range(25):
        n = rng.randint(4, 7)
        doc = {
            'source_id': f'rand-{case}', 'batch_id': 'b', 'dataset_version': 'd1',
            'quasi_identifiers': ['age', 'zip'], 'sensitive_field': 'condition',
            'hierarchies': {'age': copy.deepcopy(age_tree), 'zip': copy.deepcopy(zip_tree)},
            'rows': [{'id': f'r{i}',
                      'age': rng.choice(['20', '21', '30', '31']),
                      'zip': rng.choice(['100', '101', '200', '201']),
                      'condition': rng.choice(['A', 'B'])} for i in range(n)],
        }
        k = rng.choice([1, 2, 3])
        ell = rng.choice([1, 2])
        solution = solve(doc, k, ell)
        brute = _brute_force_optimum(doc, k, ell)
        if brute is None:
            assert solution['status'] == 'infeasible'
        else:
            assert solution['status'] == 'optimal'
            assert solution['loss'] == brute


def test_nearest_neighbour_pairing_and_global_generalization_are_both_suboptimal():
    # Values are deliberately interleaved so sequential nearest-neighbour
    # pairing crosses every tree branch.
    doc = {
        'source_id': 'nn', 'batch_id': 'b', 'dataset_version': 'd1',
        'quasi_identifiers': ['age'], 'sensitive_field': 'condition',
        'hierarchies': {'age': {
            'ancestors': {'20': ['20', '20s', '*'], '21': ['21', '20s', '*'],
                          '30': ['30', '30s', '*'], '31': ['31', '30s', '*']},
            'costs': {'20': 0, '21': 0, '30': 0, '31': 0, '20s': 1, '30s': 1, '*': 10}}},
        'rows': [{'id': f'r{i}', 'age': value, 'condition': 'A'}
                 for i, value in enumerate(['20', '30', '21', '31', '20', '30'])],
    }
    solution = solve(doc, 2, 1)
    # Exact optimum: branch-local blocks only, two pairs and one triple, cost 6.
    assert solution['status'] == 'optimal' and solution['loss'] == 6

    def nearest_neighbour_loss():
        remaining = list(range(len(doc['rows'])))
        total = 0
        while remaining:
            block = remaining[:2] if len(remaining) != 3 else remaining[:3]
            for i in block:
                remaining.remove(i)
            candidate = evaluate_class(tuple(block), doc)
            total += candidate['cost']
        return total

    # Sequential nearest-neighbour pairing crosses a branch in every block:
    # two root pairs (20 each) and one root triple (30), totalling 60, which
    # is strictly worse than the exact branch-local optimum of 6.
    assert nearest_neighbour_loss() == 60 > solution['loss']
    # Forcing one global generalization level for the whole table costs the same.
    global_node = common_ancestor([r['age'] for r in doc['rows']],
                                  doc['hierarchies']['age'])
    assert global_node == '*'
    assert len(doc['rows']) * doc['hierarchies']['age']['costs'][global_node] == 60


def test_ties_break_stably_on_frozen_row_ids_and_nodes():
    doc = load_fixture('anon-source.json')
    first = solve(doc, 2, 1)
    second = solve(copy.deepcopy(doc), 2, 1)
    assert first['partition'] == second['partition']
    publication = build_publication(doc, first['partition'])
    again = build_publication(copy.deepcopy(doc), second['partition'])
    assert publication['equivalence_classes'] == again['equivalence_classes']


def test_solver_supports_twelve_rows_three_columns_within_budget():
    doc = load_fixture('anon-source-12.json')
    solution = solve(doc, 2, 2)
    assert solution['status'] == 'optimal'
    assert solution['budget']['enumeration_truncated'] is False
    assert solution['budget']['dp_truncated'] is False
    # Every row appears exactly once in the optimal partition.
    covered = sorted(i for block in solution['partition'] for i in block)
    assert covered == list(range(12))
    publication = build_publication(doc, solution['partition'])
    assert sum(eq['size'] for eq in publication['equivalence_classes']) == 12
    assert sum(eq['loss'] for eq in publication['equivalence_classes']) == solution['loss']


# -------------------------------------- group 3: whole-table merging of equal tuples

def test_internal_groups_publishing_the_same_tuple_are_merged_for_k_and_l():
    # Every row has identical quasi-identifier values, so any internal block
    # partition publishes one and the same tuple; build_publication must merge
    # the blocks into a single actual equivalence class keyed by the tuple.
    doc = {
        'source_id': 'merge', 'batch_id': 'b', 'dataset_version': 'd1',
        'quasi_identifiers': ['age', 'zip'], 'sensitive_field': 'condition',
        'hierarchies': {
            'age': {'ancestors': {'20': ['20', '*']}, 'costs': {'20': 0, '*': 10}},
            'zip': {'ancestors': {'100': ['100', '*']}, 'costs': {'100': 0, '*': 10}}},
        'rows': [{'id': f'r{i}', 'age': 20, 'zip': 100,
                  'condition': 'A' if i % 2 else 'B'} for i in range(8)],
    }
    validate_document(doc)
    # A solver-internal partition may legitimately hold several blocks here.
    partition = tuple((i, i + 1) for i in range(0, 8, 2))
    publication = build_publication(doc, partition)
    assert len(publication['equivalence_classes']) == 1
    eq = publication['equivalence_classes'][0]
    assert eq['size'] == 8 and eq['sensitive_distinct'] == 2
    assert sorted(eq['row_ids']) == [f'r{i}' for i in range(8)]
    assert eq['qi'] == {'age': '20', 'zip': '100'} and eq['loss'] == 0
    assert len({(r['age'], r['zip']) for r in publication['published_rows']}) == 1


def test_published_tuple_is_a_true_common_ancestor_for_every_member(tmp_path):
    db = setup_source(tmp_path)
    result = anonymize.optimize(db, FIX / 'anon-opt-l1.json')
    doc = load_fixture('anon-source.json')
    chains = {field: doc['hierarchies'][field]['ancestors']
              for field in doc['quasi_identifiers']}
    by_id = {row['id']: row for row in doc['rows']}
    for audit in result['row_audit']:
        for field in doc['quasi_identifiers']:
            assert audit['published_qi'][field] in chains[field][str(by_id[audit['row_id']][field])]


# ------------------------------- group 4: bad hierarchies, infeasibility, bounds

def test_record_rejects_non_tree_cyclic_and_cost_inverting_hierarchies(tmp_path):
    base = load_fixture('anon-source.json')
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'anon-config.json')

    cyclic = copy.deepcopy(base)
    cyclic['hierarchies']['age']['ancestors']['20'] = ['20', '20s', '20']
    with pytest.raises(HierarchyError):
        anonymize.record(db, write(tmp_path, 'cyclic.json', cyclic))

    non_tree = copy.deepcopy(base)
    non_tree['hierarchies']['age']['ancestors']['20'] = ['20', '20s', '*']
    non_tree['hierarchies']['age']['ancestors']['21'] = ['21', '*', '20s']
    with pytest.raises(HierarchyError):
        anonymize.record(db, write(tmp_path, 'dag.json', non_tree))

    inverted = copy.deepcopy(base)
    inverted['hierarchies']['age']['costs']['20s'] = -1
    with pytest.raises(HierarchyError):
        anonymize.record(db, write(tmp_path, 'neg.json', inverted))

    inversion = copy.deepcopy(base)
    inversion['hierarchies']['age']['costs']['20s'] = 11  # dearer than root '*'
    with pytest.raises(HierarchyError):
        anonymize.record(db, write(tmp_path, 'inv.json', inversion))

    dangling = copy.deepcopy(base)
    dangling['hierarchies']['age']['costs']['ghost'] = 1
    with pytest.raises(HierarchyError):
        anonymize.record(db, write(tmp_path, 'dangling.json', dangling))

    missing_leaf = copy.deepcopy(base)
    del missing_leaf['hierarchies']['zip']['ancestors']['100']
    with pytest.raises((HierarchyError, ValueError), match='anon_'):
        anonymize.record(db, write(tmp_path, 'missing-leaf.json', missing_leaf))

    missing_value = copy.deepcopy(base)
    missing_value['rows'][0]['zip'] = 999
    with pytest.raises(ValueError, match='anon_missing_hierarchy_value'):
        anonymize.record(db, write(tmp_path, 'missing-value.json', missing_value))


def test_hierarchy_without_common_ancestor_makes_class_infeasible():
    spec = {'ancestors': {'x': ['x', 'X'], 'y': ['y', 'Y']},
            'costs': {'x': 0, 'y': 0, 'X': 1, 'Y': 1}}
    assert common_ancestor(['x', 'y'], spec) is None
    doc = {
        'source_id': 'disjoint', 'batch_id': 'b', 'dataset_version': 'd1',
        'quasi_identifiers': ['q'], 'sensitive_field': 's',
        'hierarchies': {'q': spec},
        'rows': [{'id': 'r1', 'q': 'x', 's': 'A'}, {'id': 'r2', 'q': 'y', 's': 'A'}],
    }
    validate_document(doc)
    assert solve(doc, 2, 1)['status'] == 'infeasible'


def test_missing_sensitive_values_do_not_count_toward_diversity(tmp_path):
    doc = load_fixture('anon-source.json')
    doc['source_id'] = 'micro-null'
    # Null out the only non-A values: every class is then single-valued, so
    # l_distinct=2 cannot be satisfied anywhere.
    for row in doc['rows']:
        if row['condition'] == 'B':
            row['condition'] = None
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'anon-config.json')
    anonymize.record(db, write(tmp_path, 'null.json', doc))
    request = {'plan_id': 'p-null', 'source_id': 'micro-null',
               'k': 2, 'l_distinct': 2}
    result = anonymize.optimize(db, write(tmp_path, 'req.json', request))
    # Missing tokens must not be silently invented to satisfy diversity.
    assert result['status'] == 'infeasible'
    request['l_distinct'] = 1
    request['plan_id'] = 'p-null-1'
    ok = anonymize.optimize(db, write(tmp_path, 'req1.json', request))
    assert ok['status'] == 'optimal'
    # Null tokens are preserved in the published table for later verification.
    null_rows = [r for r in ok['published_rows'] if r['condition'] is None]
    assert len(null_rows) == 3
    assert all(eq['sensitive_distinct'] == 1 for eq in ok['equivalence_classes'])
    assert anonymize.verify_optimization(db, 'p-null-1')['ok'] is True


def test_no_compliant_partition_is_reported_infeasible_without_fake_optimum(tmp_path):
    db = setup_source(tmp_path)
    request = write(tmp_path, 'req.json',
                    {'plan_id': 'p-k7', 'source_id': 'micro-S1',
                     'k': 7, 'l_distinct': 1})
    result = anonymize.optimize(db, request)
    assert result['status'] == 'infeasible'
    assert result['loss'] is None and result['upper_bound'] is None
    assert 'published_rows' not in result
    check = anonymize.verify_optimization(db, 'p-k7')
    assert check['ok'] is True and check['recomputed_status'] == 'infeasible'


def test_exhausted_budget_returns_indeterminate_with_proven_bounds(tmp_path):
    doc = load_fixture('anon-source-12.json')
    solution = solve(doc, 2, 2, candidate_budget=30, dp_budget=40)
    assert solution['status'] == 'indeterminate'
    assert solution['lower_bound'] <= (solution['upper_bound'] or float('inf'))
    assert solution['budget']['enumeration_truncated'] or solution['budget']['dp_truncated']
    # A generous budget must close the gap to a proven optimum.
    full = solve(doc, 2, 2)
    assert full['status'] == 'optimal'
    assert solution['lower_bound'] <= full['loss'] <= (solution['upper_bound'] or float('inf'))

    # A cut inside the DP expansion keeps full candidate classes available to
    # the greedy construction, yielding a concrete proven upper bound.
    bounded6 = solve(load_fixture('anon-source.json'), 2, 1,
                     candidate_budget=60, dp_budget=200)
    assert bounded6['status'] == 'indeterminate'
    assert bounded6['upper_bound'] == 6
    assert bounded6['lower_bound'] <= 6 <= bounded6['upper_bound']

    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'anon-config-12.json')
    anonymize.record(db, FIX / 'anon-source-12.json')
    request = write(tmp_path, 'req.json',
                    {'plan_id': 'p-bounded', 'source_id': 'micro-S2', 'k': 2,
                     'l_distinct': 2, 'candidate_budget': 30, 'dp_budget': 40})
    stored = anonymize.optimize(db, request)
    assert stored['status'] == 'indeterminate'
    assert anonymize.verify_optimization(db, 'p-bounded')['ok'] is True


def test_bad_thresholds_are_rejected(tmp_path):
    db = setup_source(tmp_path)
    for bad in ({'k': 0, 'l_distinct': 1}, {'k': 2, 'l_distinct': 0},
                {'k': True, 'l_distinct': 1}, {'k': 2.0, 'l_distinct': 1}):
        request = {'plan_id': f'bad-{bad}', 'source_id': 'micro-S1', **bad}
        with pytest.raises(ValueError, match='anon_bad_threshold'):
            anonymize.optimize(db, write(tmp_path, 'r.json', request))


# --------------------------------- group 5: version binding, replay, verify

def test_same_source_and_contract_replays_identically(tmp_path):
    db = setup_source(tmp_path)
    first = anonymize.optimize(db, FIX / 'anon-opt-l1.json')
    second = anonymize.optimize(db, FIX / 'anon-opt-l1.json')
    assert first == second


def test_same_plan_id_with_changed_contract_conflicts(tmp_path):
    db = setup_source(tmp_path)
    anonymize.optimize(db, FIX / 'anon-opt-l1.json')
    changed = load_fixture('anon-opt-l1.json')
    changed['l_distinct'] = 2
    with pytest.raises(ValueError, match='anon_plan_conflict'):
        anonymize.optimize(db, write(tmp_path, 'changed.json', changed))


def test_same_source_id_with_different_bytes_is_rejected(tmp_path):
    db = setup_source(tmp_path)
    tampered = load_fixture('anon-source.json')
    tampered['rows'][0]['condition'] = 'Z'
    with pytest.raises(ValueError, match='anon_source_conflict'):
        anonymize.record(db, write(tmp_path, 'tampered.json', tampered))


def test_dataset_version_change_blocks_rebinding(tmp_path):
    db = setup_source(tmp_path)
    moved = load_fixture('anon-source.json')
    moved['source_id'] = 'micro-other'
    moved['dataset_version'] = 'd999'
    with pytest.raises(ValueError, match='anon_dataset_mismatch'):
        anonymize.record(db, write(tmp_path, 'version.json', moved))
    unknown_batch = load_fixture('anon-source.json')
    unknown_batch['source_id'] = 'micro-x'
    unknown_batch['batch_id'] = 'no-such-batch'
    with pytest.raises(ValueError, match='anon_unknown_batch'):
        anonymize.record(db, write(tmp_path, 'batch.json', unknown_batch))


def test_old_optimization_is_not_silently_rewritten_by_a_new_source(tmp_path):
    db = setup_source(tmp_path)
    old = anonymize.optimize(db, FIX / 'anon-opt-l1.json')
    assert old['source_digest'] == anonymize.optimize(db, FIX / 'anon-opt-l1.json')['source_digest']
    # A second, independently frozen source gets its own plan row.
    second = load_fixture('anon-source-12.json')
    initialize(db, FIX / 'anon-config-12.json')
    anonymize.record(db, FIX / 'anon-source-12.json')
    other = anonymize.optimize(db, FIX / 'anon-opt-12.json')
    assert other['source_id'] == 'micro-S2'
    untouched = connect(db).execute(
        'SELECT result FROM anon_opt_plan WHERE plan_id=?', ('anon-opt-l1',)).fetchone()[0]
    assert json.loads(untouched)['source_id'] == 'micro-S1'
    assert json.loads(untouched)['loss'] == old['loss']


def _tamper_result(db, plan_id, mutate):
    row = connect(db).execute(
        'SELECT result FROM anon_opt_plan WHERE plan_id=?', (plan_id,)).fetchone()
    payload = json.loads(row['result'])
    mutate(payload)
    with connect(db) as writable:
        writable.execute('UPDATE anon_opt_plan SET result=? WHERE plan_id=?',
                         (json.dumps(payload, sort_keys=True, ensure_ascii=False), plan_id))


def test_verify_detects_forged_nodes_dropped_rows_and_suboptimal_loss(tmp_path):
    db = setup_source(tmp_path)
    anonymize.optimize(db, FIX / 'anon-opt-l2.json')

    _tamper_result(db, 'anon-opt-l2', lambda p: p['row_audit'].pop())
    assert 'anon_verify_row_set_mismatch' in anonymize.verify_optimization(
        db, 'anon-opt-l2')['problems']

    plan2 = 'tamper-node'
    req = write(tmp_path, 'node.json',
                {'plan_id': plan2, 'source_id': 'micro-S1', 'k': 2, 'l_distinct': 2})
    anonymize.optimize(db, req)

    def forge(p):
        p['equivalence_classes'][0]['qi']['age'] = '*'
    _tamper_result(db, plan2, forge)
    problems = anonymize.verify_optimization(db, plan2)['problems']
    assert 'anon_verify_forged_node' in problems

    plan3 = 'tamper-loss'
    req3 = write(tmp_path, 'loss.json',
                 {'plan_id': plan3, 'source_id': 'micro-S1', 'k': 2, 'l_distinct': 1})
    anonymize.optimize(db, req3)
    _tamper_result(db, plan3, lambda p: p.update(loss=p['loss'] + 5))
    assert 'anon_verify_suboptimal' in anonymize.verify_optimization(db, plan3)['problems']


def test_verify_detects_fake_duplicate_equivalence_class_ids(tmp_path):
    db = setup_source(tmp_path)
    plan = 'dup-eq'
    req = write(tmp_path, 'dup.json',
                {'plan_id': plan, 'source_id': 'micro-S1', 'k': 2, 'l_distinct': 1})
    anonymize.optimize(db, req)

    def duplicate(p):
        clone = copy.deepcopy(p['equivalence_classes'][0])
        clone['eq_index'] = len(p['equivalence_classes'])
        p['equivalence_classes'].append(clone)
    _tamper_result(db, plan, duplicate)
    problems = anonymize.verify_optimization(db, plan)['problems']
    assert 'anon_verify_merge_mismatch' in problems


def test_verify_detects_tampered_sql_audit_and_row_audit_mapping(tmp_path):
    db = setup_source(tmp_path)
    plan = 'sql-tamper'
    req = write(tmp_path, 'sql.json',
                {'plan_id': plan, 'source_id': 'micro-S1', 'k': 2, 'l_distinct': 1})
    anonymize.optimize(db, req)
    with connect(db) as writable:
        writable.execute('DELETE FROM anon_opt_row WHERE plan_id=? AND row_id=?',
                         (plan, 'r1'))
    assert 'anon_verify_row_table_mismatch' in anonymize.verify_optimization(
        db, plan)['problems']

    plan2 = 'audit-tamper'
    req2 = write(tmp_path, 'audit.json',
                 {'plan_id': plan2, 'source_id': 'micro-S1', 'k': 2, 'l_distinct': 1})
    anonymize.optimize(db, req2)

    def remap(p):
        p['row_audit'][0]['eq_index'] = 2
    _tamper_result(db, plan2, remap)
    assert 'anon_verify_row_audit_mismatch' in anonymize.verify_optimization(
        db, plan2)['problems']


def test_verify_recomputes_independently_from_frozen_source_bytes(tmp_path):
    db = setup_source(tmp_path)
    result = anonymize.optimize(db, FIX / 'anon-opt-l2.json')
    check = anonymize.verify_optimization(db, 'anon-opt-l2')
    assert check == {'plan_id': 'anon-opt-l2', 'ok': True, 'problems': [],
                     'recomputed_status': 'optimal', 'recomputed_loss': 14,
                     'recomputed_bounds': {'lower': 14, 'upper': 14}}
    # The verify path never trusts the stored source digest: corrupt the raw
    # bytes and confirm verification flags it rather than reusing them or
    # crashing.
    with connect(db) as writable:
        row = writable.execute('SELECT raw FROM anon_source WHERE source_id=?',
                               ('micro-S1',)).fetchone()
        broken = bytearray(row['raw'])
        broken[50] ^= 0x01
        writable.execute('UPDATE anon_source SET raw=? WHERE source_id=?',
                         (bytes(broken), 'micro-S1'))
    check = anonymize.verify_optimization(db, 'anon-opt-l2')
    assert check['ok'] is False
    assert 'anon_verify_source_digest' in check['problems']
    assert result['source_digest']


def test_manual_evaluation_entry_point_remains_available(tmp_path):
    db = setup_source(tmp_path)
    result = anonymize.evaluate_manual(db, FIX / 'anon-manual.json')
    assert result['valid'] is True and result['loss'] == 6
    assert result['groups'][0]['qi'] == {'age': '20s', 'zip': '100'}
    # Manual evaluation still does not publish any row table.
    assert 'published_rows' not in result


def test_manual_plan_with_disjoint_hierarchy_values_is_rejected(tmp_path):
    doc = {
        'source_id': 'disjoint-manual', 'batch_id': 'anon-demo',
        'dataset_version': 'd2',
        'quasi_identifiers': ['age'], 'sensitive_field': 'condition',
        'hierarchies': {'age': {
            'ancestors': {'20': ['20', 'X'], '30': ['30', 'Y']},
            'costs': {'20': 0, '30': 0, 'X': 1, 'Y': 1}}},
        'rows': [{'id': 'r1', 'age': 20, 'condition': 'A'},
                 {'id': 'r2', 'age': 30, 'condition': 'A'}],
    }
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'anon-config.json')
    anonymize.record(db, write(tmp_path, 'source.json', doc))
    plan = write(tmp_path, 'plan.json',
                 {'plan_id': 'mp', 'source_id': 'disjoint-manual',
                  'k': 2, 'l_distinct': 1, 'groups': [['r1', 'r2']]})
    with pytest.raises(ValueError, match='anon_no_common_generalization'):
        anonymize.evaluate_manual(db, plan)


def test_twelve_row_fixture_runs_through_store_and_verify(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'anon-config-12.json')
    anonymize.record(db, FIX / 'anon-source-12.json')
    result = anonymize.optimize(db, FIX / 'anon-opt-12.json')
    assert result['status'] == 'optimal'
    assert anonymize.verify_optimization(db, 'anon-opt-12')['ok'] is True
    # Three quasi-identifier columns are all represented per published row.
    assert all(set(row) == {'eq_index', 'age', 'zip', 'city', 'condition'}
               for row in result['published_rows'])
