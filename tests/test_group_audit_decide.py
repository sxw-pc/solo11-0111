import hashlib
import json
from fractions import Fraction
from itertools import product
from math import comb
from pathlib import Path

import pytest

from purposegate.group_audit import decide, inspect, record, verify
from purposegate.ingest import initialize
from purposegate.store import connect

FIX = Path(__file__).resolve().parent.parent / 'fixtures'
OBSERVED = FIX / 'group-audit-contract-observed.json'
COMMON = FIX / 'group-audit-contract-common.json'


def brute_pooled_p(a0, n0, a1, n1):
    """Independent exact check: enumerate the pooled hypergeometric support."""
    total = n0 + n1
    m = a0 + a1
    bound = abs(Fraction(a0, n0) - Fraction(a1, n1))
    p = total_p = Fraction(0)
    for k in range(max(0, n0 - (total - m)), min(m, n0) + 1):
        stat = Fraction(k, n0) - Fraction(m - k, n1)
        prob = Fraction(comb(m, k) * comb(total - m, n0 - k), comb(total, n0))
        total_p += prob
        if abs(stat) >= bound:
            p += prob
    assert total_p == 1
    return p


def brute_stratified_p(layers, weights):
    """Independent exact check: enumerate the full product space of strata."""
    supports = []
    for a0, n0, a1, n1 in layers:
        total = n0 + n1
        m = a0 + a1
        supports.append(range(max(0, n0 - (total - m)), min(m, n0) + 1))
    observed = sum(w * (Fraction(a0, n0) - Fraction(a1, n1))
                   for (a0, n0, a1, n1), w in zip(layers, weights))
    bound = abs(observed)
    p = total_p = Fraction(0)
    for combo in product(*supports):
        stat = Fraction(0)
        prob = Fraction(1)
        for k, (a0, n0, a1, n1), w in zip(combo, layers, weights):
            total = n0 + n1
            m = a0 + a1
            stat += w * (Fraction(k, n0) - Fraction(m - k, n1))
            prob *= Fraction(comb(m, k) * comb(total - m, n0 - k), comb(total, n0))
        total_p += prob
        if abs(stat) >= bound:
            p += prob
    assert total_p == 1
    return p


def write_json(path, doc):
    path.write_text(json.dumps(doc), encoding='utf-8')
    return path


def cell(group, stratum, allowed, denied):
    return {'group': group, 'stratum': stratum, 'allowed': allowed, 'denied': denied}


def source_doc(audit_id, strata, cells, batch_id='group-audit-demo',
               rule_version='audit-v1'):
    return {'audit_id': audit_id, 'batch_id': batch_id, 'rule_version': rule_version,
            'groups': ['A', 'B'], 'strata': list(strata), 'cells': cells}


def contract_doc(contract_id, audit_id, estimand, weights=None,
                 min_gap=(1, 5), max_p=(1, 20)):
    doc = {'contract_id': contract_id, 'audit_id': audit_id, 'estimand': estimand,
           'thresholds': {'min_gap': list(min_gap), 'max_p': list(max_p)}}
    if weights is not None:
        doc['weights'] = weights
    return doc


def record_doc(tmp_path, db, doc):
    return record(db, write_json(tmp_path / f'{doc["audit_id"]}.json', doc))


def decide_doc(tmp_path, db, doc):
    return decide(db, write_json(tmp_path / f'{doc.get("contract_id", "bad")}.json', doc))


def ready(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'group-audit-config.json')
    record(db, FIX / 'group-audit-matrix.json')
    return db


# --- acceptance group 1: same evidence, two estimation contracts ----------

def test_same_evidence_two_estimation_contracts_diverge(tmp_path):
    db = ready(tmp_path)
    observed = decide(db, OBSERVED)
    common = decide(db, COMMON)
    # identical frozen evidence, genuinely different investigation tasks
    assert observed['source_digest'] == common['source_digest']
    assert observed['audit_id'] == common['audit_id'] == 'audit-S1'
    assert observed['estimand'] != common['estimand']
    assert observed['estimand_definition'] != common['estimand_definition']
    # observed_mix: overall rates 82/100 vs 18/100, gap 16/25, exact p, triggers
    assert observed['group_rates'] == {'A': [41, 50], 'B': [9, 50]}
    assert observed['gap'] == [16, 25]
    p = Fraction(*observed['test']['p_value'])
    assert p == brute_pooled_p(82, 100, 18, 100)
    assert 2.1e-20 < float(p) < 2.3e-20
    assert p <= Fraction(1, 20)
    assert observed['trigger']['investigate'] is True
    assert observed['trigger']['failed'] == []
    assert observed['trigger']['conclusion'] == '触发调查'
    # common_reference: both standardized rates 1/2, gap 0, p = 1, no trigger
    assert common['group_rates'] == {'A': [1, 2], 'B': [1, 2]}
    assert common['gap'] == [0, 1]
    assert common['test']['p_value'] == [1, 1]
    assert common['trigger']['investigate'] is False
    assert common['trigger']['failed'] == ['min_gap', 'max_p']
    # within-stratum rates are identical; only the case mix differs, and the
    # frozen table makes that explicit instead of calling it a within-stratum gap
    for row in common['strata_table']:
        assert row['cells']['A']['rate'] == row['cells']['B']['rate']
    mix = {row['stratum']: row['mix_weight'] for row in observed['strata_table']}
    assert mix['easy'] == {'A': [9, 10], 'B': [1, 10]}
    assert mix['hard'] == {'A': [1, 10], 'B': [9, 10]}
    assert observed['scope_note'] == common['scope_note']
    assert verify(db) == []


# --- acceptance group 2: hypergeometric convolution and boundaries --------

def test_three_strata_convolution_matches_brute_force(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'group-audit-config.json')
    doc = source_doc('audit-3s', ['x', 'y', 'z'], [
        cell('A', 'x', 3, 2), cell('B', 'x', 1, 4),
        cell('A', 'y', 2, 2), cell('B', 'y', 3, 1),
        cell('A', 'z', 1, 2), cell('B', 'z', 2, 4),
    ])
    record_doc(tmp_path, db, doc)
    weights = {'x': [1, 3], 'y': [1, 3], 'z': [1, 3]}
    result = decide_doc(tmp_path, db, contract_doc(
        'c-3s-common', 'audit-3s', 'common_reference', weights=weights))
    layers = [(3, 5, 1, 5), (2, 4, 3, 4), (1, 3, 2, 6)]
    w = [Fraction(1, 3)] * 3
    assert Fraction(*result['test']['p_value']) == brute_stratified_p(layers, w)
    assert result['test']['p_value'] == [1363, 1372]
    assert result['group_rates'] == {'A': [43, 90], 'B': [77, 180]}
    assert result['gap'] == [1, 20]
    assert result['trigger']['investigate'] is False
    assert result['trigger']['failed'] == ['min_gap', 'max_p']
    assert len(result['test']['strata']) == 3
    # observed_mix on the same source is the pooled exact test
    pooled = decide_doc(tmp_path, db, contract_doc('c-3s-observed', 'audit-3s',
                                                   'observed_mix'))
    assert Fraction(*pooled['test']['p_value']) == brute_pooled_p(6, 12, 6, 15)
    assert verify(db) == []


def test_gap_threshold_boundary_is_inclusive(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'group-audit-config.json')
    cells = [cell('A', 's1', 30, 20), cell('A', 's2', 30, 20),
             cell('B', 's1', 20, 30), cell('B', 's2', 20, 30)]
    record_doc(tmp_path, db, source_doc('audit-gap-eq', ['s1', 's2'], cells))
    result = decide_doc(tmp_path, db, contract_doc('c-gap-eq', 'audit-gap-eq',
                                                   'observed_mix'))
    # gap is exactly 1/5: the >= threshold is met, not rounded
    assert result['trigger']['abs_gap'] == [1, 5]
    assert result['trigger']['gap_met'] is True
    assert Fraction(*result['test']['p_value']) == brute_pooled_p(60, 100, 40, 100)
    assert result['trigger']['investigate'] is True
    # one allowed fewer: 19/100 < 1/5, and the report says which condition failed
    cells[0] = cell('A', 's1', 29, 21)
    record_doc(tmp_path, db, source_doc('audit-gap-below', ['s1', 's2'], cells))
    below = decide_doc(tmp_path, db, contract_doc('c-gap-below', 'audit-gap-below',
                                                  'observed_mix', max_p=(1, 1)))
    assert below['trigger']['abs_gap'] == [19, 100]
    assert below['trigger']['investigate'] is False
    assert below['trigger']['failed'] == ['min_gap']


def test_p_threshold_boundary_is_exact(tmp_path):
    db = ready(tmp_path)
    base = decide(db, OBSERVED)
    p = Fraction(*base['test']['p_value'])
    # max_p exactly equal to the exact p: <= is inclusive, still triggers
    doc = json.loads(OBSERVED.read_text(encoding='utf-8'))
    doc['contract_id'] = 'c-p-eq'
    doc['thresholds']['max_p'] = [p.numerator, p.denominator]
    at = decide_doc(tmp_path, db, doc)
    assert at['trigger']['p_met'] is True
    assert at['trigger']['investigate'] is True
    # a hair below the exact p: no rounding rescues it
    doc['contract_id'] = 'c-p-below'
    doc['thresholds']['max_p'] = [p.numerator - 1, p.denominator]
    under = decide_doc(tmp_path, db, doc)
    assert under['trigger']['p_met'] is False
    assert under['trigger']['investigate'] is False
    assert under['trigger']['failed'] == ['max_p']
    assert verify(db) == []


def test_sparse_degenerate_and_unbalanced_strata(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'group-audit-config.json')
    # s1 has zero allows at all (degenerate hypergeometric), s2 is decided
    cells = [cell('A', 's1', 0, 5), cell('B', 's1', 0, 5),
             cell('A', 's2', 4, 1), cell('B', 's2', 1, 4)]
    record_doc(tmp_path, db, source_doc('audit-sparse', ['s1', 's2'], cells))
    weights = {'s1': [1, 2], 's2': [1, 2]}
    result = decide_doc(tmp_path, db, contract_doc(
        'c-sparse', 'audit-sparse', 'common_reference', weights=weights))
    layers = [(0, 5, 0, 5), (4, 5, 1, 5)]
    w = [Fraction(1, 2)] * 2
    assert Fraction(*result['test']['p_value']) == brute_stratified_p(layers, w)
    degenerate = result['test']['strata'][0]
    assert degenerate['margins']['allowed_total'] == 0
    assert degenerate['support'] == {'lo': 0, 'hi': 0}
    assert result['group_rates'] == {'A': [2, 5], 'B': [1, 10]}
    # extremely unbalanced stratum at the 200-row limit: 1 vs 199
    cells = [cell('A', 's1', 1, 0), cell('B', 's1', 0, 199),
             cell('A', 's2', 2, 3), cell('B', 's2', 3, 2)]
    record_doc(tmp_path, db, source_doc('audit-unbalanced', ['s1', 's2'], cells))
    result = decide_doc(tmp_path, db, contract_doc(
        'c-unbalanced', 'audit-unbalanced', 'common_reference', weights=weights))
    layers = [(1, 1, 0, 199), (2, 5, 3, 5)]
    assert Fraction(*result['test']['p_value']) == brute_stratified_p(layers, w)
    assert verify(db) == []


# --- acceptance group 3: missing stratum / sparse / illegal weights -------

def test_incomplete_matrix_and_empty_cell_rejected(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'group-audit-config.json')
    missing = source_doc('audit-missing', ['s1', 's2'], [
        cell('A', 's1', 1, 1), cell('B', 's1', 1, 1), cell('A', 's2', 1, 1)])
    with pytest.raises(ValueError, match='group_audit_incomplete_matrix'):
        record_doc(tmp_path, db, missing)
    empty = source_doc('audit-empty', ['s1', 's2'], [
        cell('A', 's1', 1, 1), cell('B', 's1', 1, 1),
        cell('A', 's2', 1, 1), cell('B', 's2', 0, 0)])
    with pytest.raises(ValueError, match='group_audit_empty_cell'):
        record_doc(tmp_path, db, empty)
    duplicate = source_doc('audit-dup', ['s1', 's1'], [
        cell('A', 's1', 1, 1), cell('B', 's1', 1, 1)])
    with pytest.raises(ValueError, match='group_audit_duplicate_label'):
        record_doc(tmp_path, db, duplicate)


def test_invalid_weights_rejected(tmp_path):
    db = ready(tmp_path)
    base = contract_doc('c-w', 'audit-S1', 'common_reference',
                        weights={'easy': [1, 2], 'hard': [1, 2]})
    bad_weights = [
        {'easy': [1, 3], 'hard': [1, 2]},          # sum != 1
        {'easy': [1, 2]},                          # missing stratum
        {'easy': [1, 2], 'hard': [1, 2], 'x': [0, 1]},  # unknown stratum
        {'easy': [-1, 2], 'hard': [3, 2]},         # negative weight
        {'easy': [1, 0], 'hard': [1, 1]},          # zero denominator
        {'easy': ['1/2', 1], 'hard': [1, 2]},      # non-integer
    ]
    for i, weights in enumerate(bad_weights):
        doc = {**base, 'contract_id': f'c-w-{i}', 'weights': weights}
        with pytest.raises(ValueError, match='group_audit_bad_weights'):
            decide_doc(tmp_path, db, doc)


def test_malformed_contracts_rejected(tmp_path):
    db = ready(tmp_path)
    with pytest.raises(ValueError, match='group_audit_unknown_estimand'):
        decide_doc(tmp_path, db, contract_doc('c-e1', 'audit-S1', 'pooled'))
    with pytest.raises(ValueError, match='group_audit_bad_contract'):
        decide_doc(tmp_path, db, contract_doc('c-e2', 'audit-S1', 'observed_mix',
                                              weights={'easy': [1, 2], 'hard': [1, 2]}))
    with pytest.raises(ValueError, match='group_audit_bad_weights'):
        decide_doc(tmp_path, db, contract_doc('c-e3', 'audit-S1', 'common_reference'))
    with pytest.raises(ValueError, match='group_audit_bad_contract'):
        decide_doc(tmp_path, db, contract_doc('c-e4', 'audit-S1', 'observed_mix',
                                              min_gap=(3, 2)))
    with pytest.raises(ValueError, match='group_audit_bad_contract'):
        decide_doc(tmp_path, db, {'audit_id': 'audit-S1', 'estimand': 'observed_mix',
                                  'thresholds': {'min_gap': [1, 5], 'max_p': [1, 20]}})


def test_contract_limits_are_business_errors(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'group-audit-config.json')
    one = source_doc('audit-1s', ['only'], [cell('A', 'only', 1, 1), cell('B', 'only', 1, 1)])
    record_doc(tmp_path, db, one)  # intake still accepts; the finite contract does not
    with pytest.raises(ValueError, match='group_audit_contract_limits'):
        decide_doc(tmp_path, db, contract_doc('c-1s', 'audit-1s', 'observed_mix'))
    many = source_doc('audit-7s', [f's{i}' for i in range(7)],
                      [cell(g, f's{i}', 1, 1) for i in range(7) for g in ('A', 'B')])
    record_doc(tmp_path, db, many)
    with pytest.raises(ValueError, match='group_audit_contract_limits'):
        decide_doc(tmp_path, db, contract_doc('c-7s', 'audit-7s', 'observed_mix'))
    over = source_doc('audit-201', ['s1', 's2'], [
        cell('A', 's1', 1, 0), cell('B', 's1', 0, 200),
        cell('A', 's2', 1, 1), cell('B', 's2', 1, 1)])
    record_doc(tmp_path, db, over)
    with pytest.raises(ValueError, match='group_audit_contract_limits'):
        decide_doc(tmp_path, db, contract_doc('c-201', 'audit-201', 'observed_mix'))
    # exactly at the limit is exact and verifiable
    at_limit = source_doc('audit-200', ['s1', 's2'], [
        cell('A', 's1', 1, 0), cell('B', 's1', 0, 199),
        cell('A', 's2', 1, 1), cell('B', 's2', 1, 1)])
    record_doc(tmp_path, db, at_limit)
    result = decide_doc(tmp_path, db, contract_doc('c-200', 'audit-200', 'observed_mix'))
    assert Fraction(*result['test']['p_value']) == brute_pooled_p(2, 3, 1, 201)
    assert verify(db) == []


# --- acceptance group 4: source and version guards ------------------------

def test_unknown_source_and_wrong_rule_version(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'group-audit-config.json')
    with pytest.raises(ValueError, match='group_audit_unknown_source'):
        decide_doc(tmp_path, db, contract_doc('c-u', 'audit-missing', 'observed_mix'))
    wrong = source_doc('audit-wrong', ['s1', 's2'],
                       [cell(g, s, 1, 1) for s in ('s1', 's2') for g in ('A', 'B')],
                       rule_version='audit-v2')
    with pytest.raises(ValueError, match='group_audit_wrong_rule_version'):
        record_doc(tmp_path, db, wrong)


def test_new_batch_rule_version_and_old_diagnostic_not_overwritten(tmp_path):
    db = ready(tmp_path)
    first = decide(db, OBSERVED)
    config2 = {'batch_id': 'group-audit-v2', 'rule_version': 'audit-v2',
               'dataset': {'version': 'd1', 'fields': ['decision_status']},
               'subjects': {}}
    initialize(db, write_json(tmp_path / 'config2.json', config2))
    src2 = source_doc('audit-S2', ['easy', 'hard'], [
        cell('A', 'easy', 81, 9), cell('B', 'easy', 9, 1),
        cell('A', 'hard', 1, 9), cell('B', 'hard', 9, 81),
    ], batch_id='group-audit-v2', rule_version='audit-v2')
    record_doc(tmp_path, db, src2)
    second = decide_doc(tmp_path, db, contract_doc('contract-S2-observed-mix',
                                                   'audit-S2', 'observed_mix'))
    assert second['rule_version'] == 'audit-v2'
    assert second['batch_id'] == 'group-audit-v2'
    assert second['trigger']['investigate'] is True
    # the old diagnostic is frozen under the old rule version, not overwritten
    assert inspect(db, 'contract-S1-observed-mix') == first
    assert inspect(db, 'contract-S1-observed-mix')['rule_version'] == 'audit-v1'
    # same source id with different bytes is a conflict, never an overwrite
    altered = json.loads((FIX / 'group-audit-matrix.json').read_text(encoding='utf-8'))
    altered['cells'][0]['allowed'] += 1
    with pytest.raises(ValueError, match='group_audit_source_conflict'):
        record(db, write_json(tmp_path / 'altered.json', altered))
    # a new source may not reuse a frozen contract id either
    clash = contract_doc('contract-S1-observed-mix', 'audit-S2', 'observed_mix')
    with pytest.raises(ValueError, match='group_audit_contract_conflict'):
        decide_doc(tmp_path, db, clash)
    assert inspect(db, 'contract-S1-observed-mix') == first
    assert verify(db) == []


# --- acceptance group 5: idempotency and independent review ---------------

def test_decide_replay_is_idempotent(tmp_path):
    db = ready(tmp_path)
    first = decide(db, OBSERVED)
    again = decide(db, OBSERVED)
    assert again == first
    con = connect(db)
    rows = con.execute('SELECT created_at FROM group_audit_diagnostic').fetchall()
    con.close()
    assert len(rows) == 1
    changed = json.loads(OBSERVED.read_text(encoding='utf-8'))
    changed['thresholds']['max_p'] = [1, 10]
    with pytest.raises(ValueError, match='group_audit_contract_conflict'):
        decide(db, write_json(tmp_path / 'changed.json', changed))
    assert inspect(db, 'contract-S1-observed-mix') == first
    assert verify(db) == []


def test_frozen_report_independently_recomputable(tmp_path):
    db = ready(tmp_path)
    decide(db, OBSERVED)
    decide(db, COMMON)
    observed = inspect(db, 'contract-S1-observed-mix')
    # recompute p from the frozen margins alone, not from the source rows
    margins = observed['test']['margins']
    a0 = observed['test']['observed']['allowed_of_first_group']
    n0 = margins['group_total']['A']
    n1 = margins['group_total']['B']
    a1 = margins['allowed_total'] - a0
    assert Fraction(*observed['test']['p_value']) == brute_pooled_p(a0, n0, a1, n1)
    lower = Fraction(*observed['test']['tails']['lower'])
    upper = Fraction(*observed['test']['tails']['upper'])
    assert lower + upper == Fraction(*observed['test']['p_value'])
    common = inspect(db, 'contract-S1-common-reference')
    # recompute the convolution from the frozen strata table and weights alone
    cells = {row['stratum']: row['cells'] for row in common['strata_table']}
    layers = [(cells[s]['A']['allowed'], cells[s]['A']['total'],
               cells[s]['B']['allowed'], cells[s]['B']['total'])
              for s in common['strata']]
    weights = [Fraction(*row['weight']) for row in common['strata_table']]
    assert Fraction(*common['test']['p_value']) == brute_stratified_p(layers, weights)
    assert verify(db) == []


def test_verify_detects_tampered_diagnostic(tmp_path):
    db = ready(tmp_path)
    decide(db, OBSERVED)
    assert verify(db) == []
    con = connect(db)
    frozen = json.loads(con.execute(
        "SELECT result_json FROM group_audit_diagnostic "
        "WHERE contract_id='contract-S1-observed-mix'").fetchone()['result_json'])
    frozen['trigger']['investigate'] = False
    frozen['test']['p_value'] = [1, 1]
    with con:
        con.execute("UPDATE group_audit_diagnostic SET result_json=? "
                    "WHERE contract_id='contract-S1-observed-mix'",
                    (json.dumps(frozen, ensure_ascii=False, sort_keys=True),))
    con.close()
    assert verify(db) == ['group_audit_diagnostic:contract-S1-observed-mix']
    assert verify(db, 'contract-S1-observed-mix') == [
        'group_audit_diagnostic:contract-S1-observed-mix']


def test_verify_detects_tampered_source_bytes(tmp_path):
    db = ready(tmp_path)
    decide(db, OBSERVED)
    con = connect(db)
    with con:
        con.execute("UPDATE group_audit_source SET raw=? WHERE audit_id='audit-S1'",
                    (b'{"tampered": true}',))
    con.close()
    assert verify(db) == ['group_audit_source_digest:contract-S1-observed-mix']


def test_verify_detects_replaced_source_with_fresh_digest(tmp_path):
    db = ready(tmp_path)
    decide(db, OBSERVED)
    raw = json.dumps(source_doc('audit-S1', ['easy', 'hard'], [
        cell('A', 'easy', 80, 10), cell('B', 'easy', 9, 1),
        cell('A', 'hard', 1, 9), cell('B', 'hard', 9, 81)])).encode()
    con = connect(db)
    with con:
        con.execute("UPDATE group_audit_source SET raw=?, digest=? WHERE audit_id='audit-S1'",
                    (raw, hashlib.sha256(raw).hexdigest()))
    con.close()
    assert verify(db) == ['group_audit_diagnostic_source:contract-S1-observed-mix']


def test_verify_detects_tampered_contract(tmp_path):
    db = ready(tmp_path)
    decide(db, COMMON)
    con = connect(db)
    with con:
        con.execute("UPDATE group_audit_contract SET raw=? "
                    "WHERE contract_id='contract-S1-common-reference'",
                    (b'{"weights": "rigged"}',))
    con.close()
    assert verify(db) == ['group_audit_contract_digest:contract-S1-common-reference']
    # a replaced contract with a fresh digest disagrees with the frozen diagnostic
    raw = json.dumps(contract_doc('contract-S1-common-reference', 'audit-S1',
                                  'common_reference',
                                  weights={'easy': [1, 3], 'hard': [2, 3]})).encode()
    con = connect(db)
    with con:
        con.execute("UPDATE group_audit_contract SET raw=?, digest=? "
                    "WHERE contract_id='contract-S1-common-reference'",
                    (raw, hashlib.sha256(raw).hexdigest()))
    con.close()
    assert verify(db) == ['group_audit_diagnostic_contract:contract-S1-common-reference']


def test_verify_unknown_diagnostic_raises(tmp_path):
    db = ready(tmp_path)
    with pytest.raises(ValueError, match='group_audit_unknown_diagnostic'):
        verify(db, 'no-such-contract')
    assert verify(db) == []
