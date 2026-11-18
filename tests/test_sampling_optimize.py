"""Acceptance tests for sample-optimize / sample-verify.

Five groups: dual-contract roster differences, three-stratum anti-greedy,
budget and boundary/infeasible inputs, provenance/version/idempotency, and
independent verification.
"""
import hashlib
import json
from fractions import Fraction
from pathlib import Path

import pytest

from purposegate.ingest import initialize
from purposegate.sampling import (
    evaluate_manual,
    optimize_plan,
    record_frame,
    verify_plan,
)
from purposegate.store import connect

FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def base_db(tmp_path, *frame_names):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'sample-config.json')
    for name in frame_names:
        record_frame(db, FIX / name)
    return db


def write_doc(tmp_path, doc, name='doc.json'):
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding='utf-8')
    return path


def frame_doc(**overrides):
    doc = json.loads((FIX / 'sample-frame.json').read_text(encoding='utf-8'))
    doc.update(overrides)
    return doc


def contract_doc(**overrides):
    doc = {'plan_id': 'p1', 'frame_id': 'frame-S1', 'objective': 'min_design_variance'}
    doc.update(overrides)
    return doc


def stored_result(db, plan_id):
    con = connect(db)
    row = con.execute('SELECT result FROM sample_optimize_plan WHERE plan_id=?',
                      (plan_id,)).fetchone()
    con.close()
    return json.loads(row['result'])


def patch_result(db, plan_id, result):
    con = connect(db)
    with con:
        con.execute('UPDATE sample_optimize_plan SET result=? WHERE plan_id=?',
                    (json.dumps(result, sort_keys=True), plan_id))
    con.close()


# --- 验收组 1：双合同实际样本差异 -------------------------------------------

def test_dual_contract_draws_different_real_rows(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    var = optimize_plan(db, FIX / 'sample-optimize-variance.json')
    cov = optimize_plan(db, FIX / 'sample-optimize-coverage.json')
    assert var['status'] == cov['status'] == 'optimal'

    assert var['allocation'] == {'A': 1, 'B': 3}
    assert (var['variance_num'], var['variance_den']) == (7, 12)
    assert (var['coverage_gap_num'], var['coverage_gap_den']) == (1, 3)
    assert var['selected']['A']['ids'] == ['A4']
    assert var['selected']['B']['ids'] == ['B6', 'B5', 'B1']
    assert var['spent'] == 4 and var['total_rows'] == 4

    assert cov['allocation'] == {'A': 2, 'B': 2}
    assert (cov['variance_num'], cov['variance_den']) == (5, 6)
    assert (cov['coverage_gap_num'], cov['coverage_gap_den']) == (0, 1)
    assert cov['selected']['A']['ids'] == ['A4', 'A3']
    assert cov['selected']['B']['ids'] == ['B6', 'B5']

    # The contract change moves real rows and inclusion rates, not just labels.
    assert 'B1' in var['selected']['B']['ids']
    assert 'B1' not in cov['selected']['B']['ids']
    assert 'A3' in cov['selected']['A']['ids']
    assert 'A3' not in var['selected']['A']['ids']
    assert (var['selected']['A']['inclusion_num'],
            var['selected']['A']['inclusion_den']) == (1, 6)
    assert (cov['selected']['A']['inclusion_num'],
            cov['selected']['A']['inclusion_den']) == (1, 3)
    assert (var['selected']['B']['inclusion_num'],
            var['selected']['B']['inclusion_den']) == (1, 2)
    assert (cov['selected']['B']['inclusion_num'],
            cov['selected']['B']['inclusion_den']) == (1, 3)


def test_manual_evaluation_still_available(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    optimize_plan(db, FIX / 'sample-optimize-variance.json')
    manual = evaluate_manual(db, FIX / 'sample-manual.json')
    assert manual['feasible'] is True
    assert (manual['variance_num'], manual['variance_den']) == (5, 6)
    assert manual['selected']['A']['ids'] == ['A4', 'A3']


# --- 验收组 2：三层反贪心 ----------------------------------------------------

def greedy_marginal(counts, pilots, costs, budget):
    """Local greedy: repeatedly add the row with the largest marginal
    variance reduction per cost (ties to the lowest stratum index)."""
    total = sum(counts)

    def term(h, n):
        weight = Fraction(counts[h], total)
        return weight * weight * pilots[h] * Fraction(counts[h] - n, counts[h] * n)

    ns = [1] * len(counts)
    spent = sum(costs)
    while True:
        best = None
        for h in range(len(ns)):
            if ns[h] < counts[h] and spent + costs[h] <= budget:
                gain = (term(h, ns[h]) - term(h, ns[h] + 1)) / costs[h]
                if best is None or gain > best[0]:
                    best = (gain, h)
        if best is None:
            return ns, sum(term(h, ns[h]) for h in range(len(ns)))
        ns[best[1]] += 1
        spent += costs[best[1]]


def test_three_stratum_anti_greedy(tmp_path):
    db = base_db(tmp_path, 'sample-frame-greedy.json')
    result = optimize_plan(db, FIX / 'sample-optimize-greedy.json')
    assert result['status'] == 'optimal'
    # Global optimum [2,3,2] chosen by the stable lex rule among ties.
    assert result['allocation'] == {'G1': 2, 'G2': 3, 'G3': 2}
    assert (result['variance_num'], result['variance_den']) == (11, 135)
    assert (result['coverage_gap_num'], result['coverage_gap_den']) == (1, 5)
    assert result['spent'] == 13
    assert result['selected']['G1']['ids'] == ['G1-4', 'G1-3']
    assert result['selected']['G2']['ids'] == ['G2-1', 'G2-2', 'G2-3']
    assert result['selected']['G3']['ids'] == ['G3-1', 'G3-5']

    # The local greedy allocation is feasible but strictly worse; the
    # optimizer must not pass it off as optimal.
    greedy_ns, greedy_var = greedy_marginal([5, 5, 5], [Fraction(1)] * 3, [1, 1, 4], 13)
    assert greedy_ns == [5, 4, 1]
    assert greedy_var == Fraction(17, 180)
    assert Fraction(11, 135) < greedy_var
    assert [result['allocation'][k] for k in ('G1', 'G2', 'G3')] != greedy_ns


# --- 验收组 3：预算和边界/无解 -----------------------------------------------

def test_budget_below_minimum_rejected(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    path = write_doc(tmp_path, frame_doc(frame_id='frame-poor', budget=1))
    record_frame(db, path)
    plan = write_doc(tmp_path, contract_doc(frame_id='frame-poor'), 'plan.json')
    with pytest.raises(ValueError, match='sample_budget_below_minimum'):
        optimize_plan(db, plan)


def test_exact_min_budget_forces_one_per_stratum(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    path = write_doc(tmp_path, frame_doc(frame_id='frame-tight', budget=2))
    record_frame(db, path)
    plan = write_doc(tmp_path, contract_doc(frame_id='frame-tight'), 'plan.json')
    result = optimize_plan(db, plan)
    assert result['allocation'] == {'A': 1, 'B': 1}
    assert result['spent'] == 2


def test_census_budget_zeroes_variance(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    path = write_doc(tmp_path, frame_doc(frame_id='frame-census', budget=12))
    record_frame(db, path)
    plan = write_doc(tmp_path, contract_doc(frame_id='frame-census'), 'plan.json')
    result = optimize_plan(db, plan)
    assert result['allocation'] == {'A': 6, 'B': 6}
    assert (result['variance_num'], result['variance_den']) == (0, 1)
    assert (result['coverage_gap_num'], result['coverage_gap_den']) == (0, 1)
    assert len(result['selected']['A']['ids']) == 6


def test_unspent_remainder_is_allowed(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    doc = frame_doc(frame_id='frame-odd', budget=5)
    for spec in doc['strata']:
        spec['cost_per_row'] = 2
    record_frame(db, write_doc(tmp_path, doc))
    plan = write_doc(tmp_path, contract_doc(frame_id='frame-odd'), 'plan.json')
    result = optimize_plan(db, plan)
    assert result['allocation'] == {'A': 1, 'B': 1}
    assert result['spent'] == 4 <= result['budget']


def test_step_limit_reports_indeterminate_with_bounds(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    for objective in ('min_design_variance', 'min_coverage_gap'):
        plan = write_doc(tmp_path, contract_doc(plan_id=f'p-{objective}', objective=objective,
                                                step_limit=1),
                         f'{objective}.json')
        result = optimize_plan(db, plan)
        assert result['status'] == 'indeterminate'
        assert result['reason'] == 'sample_step_limit_exceeded'
        assert 'allocation' not in result and 'selected' not in result
        bounds = result['bounds']
        if objective == 'min_design_variance':
            assert (bounds['variance']['lower_num'], bounds['variance']['lower_den']) == (7, 12)
        else:
            assert (bounds['coverage_gap']['lower_num'],
                    bounds['coverage_gap']['lower_den']) == (0, 1)
            assert bounds['total_rows'] == {'lower': 4, 'upper': 4}
        verdict = verify_plan(db, f'p-{objective}')
        assert verdict['ok'] is True


def test_unknown_frame_objective_and_plan_conflict(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    plan = write_doc(tmp_path, contract_doc(frame_id='frame-nope'), 'plan.json')
    with pytest.raises(ValueError, match='sample_unknown_frame'):
        optimize_plan(db, plan)
    plan = write_doc(tmp_path, contract_doc(objective='fastest'), 'plan.json')
    with pytest.raises(ValueError, match='sample_unknown_objective'):
        optimize_plan(db, plan)
    optimize_plan(db, FIX / 'sample-optimize-variance.json')
    other = write_doc(tmp_path, {'plan_id': 'sample-opt-variance-1', 'frame_id': 'frame-S1',
                                 'objective': 'min_coverage_gap'}, 'other.json')
    with pytest.raises(ValueError, match='sample_optimize_conflict'):
        optimize_plan(db, other)


def test_intake_rejects_bad_cost_variance_rows_and_strata(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'sample-config.json')
    bad_cost = frame_doc(frame_id='f-bad-cost')
    bad_cost['strata'][0]['cost_per_row'] = 0
    with pytest.raises(ValueError, match='sample_bad_positive_integer'):
        record_frame(db, write_doc(tmp_path, bad_cost, 'a.json'))
    bad_pilot = frame_doc(frame_id='f-bad-pilot')
    bad_pilot['strata'][1]['pilot_variance_num'] = -9
    with pytest.raises(ValueError, match='sample_bad_positive_integer'):
        record_frame(db, write_doc(tmp_path, bad_pilot, 'b.json'))
    dup = frame_doc(frame_id='f-dup')
    dup['rows'][1] = dict(dup['rows'][0])
    with pytest.raises(ValueError, match='sample_bad_row'):
        record_frame(db, write_doc(tmp_path, dup, 'c.json'))
    empty = frame_doc(frame_id='f-empty')
    empty['rows'] = [row for row in empty['rows'] if row['stratum'] == 'A']
    with pytest.raises(ValueError, match='sample_empty_stratum'):
        record_frame(db, write_doc(tmp_path, empty, 'd.json'))


# --- 验收组 4：来源/版本/幂等 -------------------------------------------------

def test_replay_is_idempotent(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    first = optimize_plan(db, FIX / 'sample-optimize-variance.json')
    second = optimize_plan(db, FIX / 'sample-optimize-variance.json')
    assert first == second
    con = connect(db)
    count = con.execute('SELECT count(*) FROM sample_optimize_plan').fetchone()[0]
    con.close()
    assert count == 1


def test_new_frame_writes_new_plan_and_keeps_old(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    before = optimize_plan(db, FIX / 'sample-optimize-variance.json')
    record_frame(db, FIX / 'sample-frame-greedy.json')
    greedy = optimize_plan(db, FIX / 'sample-optimize-greedy.json')
    assert greedy['frame_id'] == 'frame-anti-greedy'
    replay = optimize_plan(db, FIX / 'sample-optimize-variance.json')
    assert replay == before
    assert stored_result(db, 'sample-opt-variance-1') == before


def test_provenance_binds_frame_contract_and_seed(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    result = optimize_plan(db, FIX / 'sample-optimize-variance.json')
    provenance = result['provenance']
    raw = (FIX / 'sample-frame.json').read_bytes()
    contract_raw = (FIX / 'sample-optimize-variance.json').read_bytes()
    assert provenance['frame_digest'] == hashlib.sha256(raw).hexdigest()
    assert provenance['plan_digest'] == hashlib.sha256(contract_raw).hexdigest()
    assert provenance['priority_seed'] == 'synthetic-fixed-seed'
    assert provenance['dataset_version'] == 'd2'
    assert provenance['batch_id'] == 'sample-demo'
    assert result['optimality']['steps'] <= result['optimality']['step_limit']


def test_dataset_version_and_unknown_batch_rejected(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'sample-config.json')
    wrong_version = frame_doc(frame_id='f-version', dataset_version='d9')
    with pytest.raises(ValueError, match='sample_dataset_mismatch'):
        record_frame(db, write_doc(tmp_path, wrong_version, 'v.json'))
    wrong_batch = frame_doc(frame_id='f-batch', batch_id='nope')
    with pytest.raises(ValueError, match='sample_unknown_batch'):
        record_frame(db, write_doc(tmp_path, wrong_batch, 'b.json'))
    conflict = frame_doc(dataset_version='d2')
    record_frame(db, write_doc(tmp_path, conflict, 'ok.json'))
    conflict['budget'] = 3
    with pytest.raises(ValueError, match='sample_frame_conflict'):
        record_frame(db, write_doc(tmp_path, conflict, 'conflict.json'))


# --- 验收组 5：独立 verify ----------------------------------------------------

def test_verify_accepts_genuine_plans(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json', 'sample-frame-greedy.json')
    for fixture in ('sample-optimize-variance.json', 'sample-optimize-coverage.json',
                    'sample-optimize-greedy.json'):
        result = optimize_plan(db, FIX / fixture)
        verdict = verify_plan(db, result['plan_id'])
        assert verdict['ok'] is True
        assert verdict['problems'] == []
        assert verdict['recomputed'] == result


def test_verify_detects_forged_better_objective(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    optimize_plan(db, FIX / 'sample-optimize-variance.json')
    forged = stored_result(db, 'sample-opt-variance-1')
    forged['variance_num'] = 1  # claims 1/12 < true optimum 7/12
    patch_result(db, 'sample-opt-variance-1', forged)
    verdict = verify_plan(db, 'sample-opt-variance-1')
    assert verdict['ok'] is False
    assert 'sample_verify_objective_value' in verdict['problems']


def test_verify_detects_roster_reuse_and_seed_replacement(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    optimize_plan(db, FIX / 'sample-optimize-variance.json')
    reused = stored_result(db, 'sample-opt-variance-1')
    reused['selected']['B']['ids'] = ['B6', 'B6', 'B5']
    patch_result(db, 'sample-opt-variance-1', reused)
    verdict = verify_plan(db, 'sample-opt-variance-1')
    assert 'sample_verify_roster_reused_id' in verdict['problems']
    assert 'sample_verify_roster' in verdict['problems']

    optimize_plan(db, FIX / 'sample-optimize-coverage.json')
    reseeded = stored_result(db, 'sample-opt-coverage-1')
    seed = 'replaced-seed'

    def order(members):
        return sorted(members, key=lambda m: (
            hashlib.sha256((seed + '\0' + m).encode()).hexdigest(), m))

    reseeded['selected']['A']['ids'] = order(['A1', 'A2', 'A3', 'A4', 'A5', 'A6'])[:2]
    reseeded['selected']['B']['ids'] = order(['B1', 'B2', 'B3', 'B4', 'B5', 'B6'])[:2]
    patch_result(db, 'sample-opt-coverage-1', reseeded)
    verdict = verify_plan(db, 'sample-opt-coverage-1')
    assert 'sample_verify_roster' in verdict['problems']


def test_verify_detects_cost_misuse(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    optimize_plan(db, FIX / 'sample-optimize-variance.json')
    tampered = stored_result(db, 'sample-opt-variance-1')
    tampered['spent'] = 3
    patch_result(db, 'sample-opt-variance-1', tampered)
    verdict = verify_plan(db, 'sample-opt-variance-1')
    assert 'sample_verify_cost' in verdict['problems']


def test_verify_detects_frame_tamper_and_unknown_plan(tmp_path):
    db = base_db(tmp_path, 'sample-frame.json')
    optimize_plan(db, FIX / 'sample-optimize-variance.json')
    con = connect(db)
    raw = con.execute('SELECT raw FROM sample_frame WHERE frame_id=?',
                      ('frame-S1',)).fetchone()['raw']
    doc = json.loads(raw)
    doc['budget'] = 99
    with con:
        con.execute('UPDATE sample_frame SET raw=? WHERE frame_id=?',
                    (json.dumps(doc).encode(), 'frame-S1'))
    con.close()
    verdict = verify_plan(db, 'sample-opt-variance-1')
    assert 'sample_verify_frame_digest' in verdict['problems']
    with pytest.raises(ValueError, match='sample_unknown_plan'):
        verify_plan(db, 'plan-nope')
