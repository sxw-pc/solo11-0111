import json
from pathlib import Path

import pytest

from purposegate.ingest import initialize
from purposegate.matching import decide, evaluate_manual, record, verify_certificate
from purposegate.store import connect

FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def _db(tmp_path, *manifests):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'link-config.json')
    for name in manifests:
        record(db, FIX / name)
    return db


def _write(tmp_path, doc, name):
    path = tmp_path / name
    path.write_text(json.dumps(doc), encoding='utf-8')
    return path


def _pairs(cert):
    return [(e['left'], e['right']) for e in cert['canonical_entities']]


def _tamper(db, certificate_id, mutate):
    con = connect(db)
    row = con.execute('SELECT result FROM link_certificate WHERE certificate_id=?',
                      (certificate_id,)).fetchone()
    cert = json.loads(row['result'])
    mutate(cert)
    con.execute('UPDATE link_certificate SET result=? WHERE certificate_id=?',
                (json.dumps(cert, sort_keys=True), certificate_id))
    con.commit()
    con.close()


# 验收组 1：两种复核合同的真实配对确实不同
def test_dual_contracts_change_real_pairings(tmp_path):
    db = _db(tmp_path, 'link-manifest.json')
    total = decide(db, FIX / 'link-decide-total.json')
    minimax = decide(db, FIX / 'link-decide-min.json')
    assert total['status'] == minimax['status'] == 'optimal'
    assert _pairs(total) == [('a1', 'b1'), ('a2', 'b2'), ('a3', 'b3')]
    assert (total['total_score'], total['minimum_score']) == (25, 5)
    assert _pairs(minimax) == [('a1', 'b2'), ('a2', 'b3'), ('a3', 'b1')]
    assert (minimax['total_score'], minimax['minimum_score']) == (24, 8)
    # 三个规范实体的真实配对全部变化，不只是换目标标签
    total_map = dict(_pairs(total))
    minimax_map = dict(_pairs(minimax))
    assert all(total_map[l] != minimax_map[l] for l in ('a1', 'a2', 'a3'))
    assert {e['entity_key'] for e in total['canonical_entities']}.isdisjoint(
        {e['entity_key'] for e in minimax['canonical_entities']})
    assert total['source_summary'] == {'matcher-A': 3, 'matcher-B': 3}
    assert total['optimality']['ranked_objective'] == [25, 5]
    assert minimax['optimality']['ranked_objective'] == [8, 24]
    assert verify_certificate(db, total['certificate_id'])['ok']
    assert verify_certificate(db, minimax['certificate_id'])['ok']


# 验收组 2：逐边最高分贪心不是全局最优
def test_greedy_counterexample(tmp_path):
    db = _db(tmp_path, 'link-greedy-manifest.json')
    cert = decide(db, FIX / 'link-greedy-decide.json')
    assert _pairs(cert) == [('a1', 'b2'), ('a2', 'b1')]
    assert (cert['total_score'], cert['minimum_score']) == (16, 8)
    # 逐边贪心先拿最高分 a1-b1=9，只剩 a2-b2=1，总分 10
    greedy_plan = _write(tmp_path, {'plan_id': 'greedy-manual', 'manifest_id': 'link-G1',
                                    'pairs': [{'left': 'a1', 'right': 'b1'},
                                              {'left': 'a2', 'right': 'b2'}]}, 'greedy.json')
    greedy = evaluate_manual(db, greedy_plan)
    assert greedy['total_score'] == 10
    assert cert['total_score'] > greedy['total_score']
    assert verify_certificate(db, cert['certificate_id'])['ok']


# 验收组 3：同边两来源分值不同取低值，最优配对因此因果变化
def test_divergent_claims_take_minimum_and_flip_optimum(tmp_path):
    db = _db(tmp_path, 'link-spread-manifest.json')
    cert = decide(db, FIX / 'link-spread-decide.json')
    # a1-b1 两份声明 10 与 3，保守分取 3；直连 3+9=12 输给交叉 8+8=16
    # （若误取高值 10，直连 10+9=19 会被错选）
    assert _pairs(cert) == [('a1', 'b2'), ('a2', 'b1')]
    assert (cert['total_score'], cert['minimum_score']) == (16, 8)
    direct_plan = _write(tmp_path, {'plan_id': 'spread-manual', 'manifest_id': 'link-D1',
                                    'pairs': [{'left': 'a1', 'right': 'b1'},
                                              {'left': 'a2', 'right': 'b2'}]}, 'direct.json')
    direct = evaluate_manual(db, direct_plan)
    assert direct['total_score'] == 12
    pair = direct['pairs'][0]
    assert (pair['left'], pair['right']) == ('a1', 'b1')
    assert pair['score'] == 3
    assert pair['claims'] == ['d11a', 'd11b']
    assert direct['total_score'] < cert['total_score']
    assert verify_certificate(db, cert['certificate_id'])['ok']


# 验收组 4：冲突输入与无完美匹配
def test_duplicate_claim_and_single_source_edge_rejected(tmp_path):
    db = _db(tmp_path)
    doc = json.loads((FIX / 'link-greedy-manifest.json').read_text(encoding='utf-8'))
    doc['manifest_id'] = 'link-bad-dup'
    doc['edges'][1]['claims'][0]['id'] = doc['edges'][0]['claims'][0]['id']
    with pytest.raises(ValueError, match='link_duplicate_claim'):
        record(db, _write(tmp_path, doc, 'dup.json'))
    doc = json.loads((FIX / 'link-greedy-manifest.json').read_text(encoding='utf-8'))
    doc['manifest_id'] = 'link-bad-single'
    doc['edges'][0]['claims'] = doc['edges'][0]['claims'][:1]
    with pytest.raises(ValueError, match='link_insufficient_claims'):
        record(db, _write(tmp_path, doc, 'single.json'))


def test_manifest_conflict_and_size_limit(tmp_path):
    db = _db(tmp_path, 'link-manifest.json')
    doc = json.loads((FIX / 'link-manifest.json').read_text(encoding='utf-8'))
    doc['edges'][0]['claims'][0]['score'] = 9  # 相同 manifest ID 异文
    with pytest.raises(ValueError, match='link_manifest_conflict'):
        record(db, _write(tmp_path, doc, 'conflict.json'))
    big = {'manifest_id': 'link-big', 'batch_id': 'link-demo', 'dataset_version': 'd2',
           'left_ids': [f'l{i}' for i in range(13)],
           'right_ids': [f'r{i}' for i in range(13)], 'edges': []}
    with pytest.raises(ValueError, match='link_manifest_too_large'):
        record(db, _write(tmp_path, big, 'big.json'))


def test_no_perfect_matching_reports_blocking_cut(tmp_path):
    db = _db(tmp_path, 'link-blocked-manifest.json')
    cert = decide(db, FIX / 'link-blocked-decide.json')
    assert cert['status'] == 'no_perfect_matching'
    assert cert['canonical_entities'] == []
    assert 'total_score' not in cert
    assert cert['blocking_cut'] == {'left_set': ['a1', 'a2'], 'neighborhood': ['b1'],
                                    'deficiency': 1, 'proof': 'hall_violator'}
    result = verify_certificate(db, cert['certificate_id'])
    assert result['ok'], result['problems']


def test_certificate_plan_conflict(tmp_path):
    db = _db(tmp_path, 'link-manifest.json')
    decide(db, FIX / 'link-decide-total.json')
    plan = json.loads((FIX / 'link-decide-total.json').read_text(encoding='utf-8'))
    plan['contract'] = 'max_min_pair_score'
    with pytest.raises(ValueError, match='link_certificate_conflict'):
        decide(db, _write(tmp_path, plan, 'other-plan.json'))


# 验收组 5：版本、幂等与独立 verify
def test_replay_is_idempotent(tmp_path):
    db = _db(tmp_path, 'link-manifest.json')
    first = decide(db, FIX / 'link-decide-total.json')
    second = decide(db, FIX / 'link-decide-total.json')
    assert first == second
    con = connect(db)
    assert con.execute('SELECT count(*) FROM link_certificate').fetchone()[0] == 1
    con.close()


def test_new_source_version_keeps_old_certificate(tmp_path):
    db = _db(tmp_path, 'link-manifest.json')
    old = decide(db, FIX / 'link-decide-total.json')
    doc = json.loads((FIX / 'link-manifest.json').read_text(encoding='utf-8'))
    doc['manifest_id'] = 'link-S2'
    for edge in doc['edges']:
        if (edge['left'], edge['right']) == ('a3', 'b3'):
            for claim in edge['claims']:
                claim['score'] = 6
    record(db, _write(tmp_path, doc, 'v2.json'))
    new = decide(db, _write(tmp_path, {'certificate_id': 'link-cert-total-2',
                                       'manifest_id': 'link-S2',
                                       'contract': 'max_total_score'}, 'plan2.json'))
    assert new['total_score'] == 26
    again = verify_certificate(db, old['certificate_id'])
    assert again['ok'], again['problems']
    con = connect(db)
    stored = json.loads(con.execute(
        'SELECT result FROM link_certificate WHERE certificate_id=?',
        (old['certificate_id'],)).fetchone()['result'])
    con.close()
    assert stored['total_score'] == 25


def test_verify_detects_forged_claim(tmp_path):
    db = _db(tmp_path, 'link-manifest.json')
    cert = decide(db, FIX / 'link-decide-total.json')
    _tamper(db, cert['certificate_id'],
            lambda c: c['canonical_entities'][0]['claims'][0].update(score=7))
    result = verify_certificate(db, cert['certificate_id'])
    assert not result['ok']
    assert 'forged_claim' in result['problems']


def test_verify_detects_duplicate_right(tmp_path):
    db = _db(tmp_path, 'link-manifest.json')
    cert = decide(db, FIX / 'link-decide-total.json')
    _tamper(db, cert['certificate_id'],
            lambda c: c['canonical_entities'][1].update(
                right=c['canonical_entities'][0]['right']))
    result = verify_certificate(db, cert['certificate_id'])
    assert not result['ok']
    assert 'duplicate_right' in result['problems']


def test_verify_detects_suboptimal_mapping(tmp_path):
    db = _db(tmp_path, 'link-manifest.json')
    total = decide(db, FIX / 'link-decide-total.json')
    minimax = decide(db, FIX / 'link-decide-min.json')

    def swap(cert):
        cert['canonical_entities'] = minimax['canonical_entities']
        cert['total_score'] = 24
        cert['minimum_score'] = 8

    _tamper(db, total['certificate_id'], swap)
    result = verify_certificate(db, total['certificate_id'])
    assert not result['ok']
    assert 'suboptimal_mapping' in result['problems']


def test_budget_exhaustion_reports_indeterminate_with_bounds(tmp_path):
    db = _db(tmp_path, 'link-manifest.json')
    plan = _write(tmp_path, {'certificate_id': 'link-cert-tight',
                             'manifest_id': 'link-S1',
                             'contract': 'max_total_score', 'budget': 1}, 'tight.json')
    cert = decide(db, plan)
    assert cert['status'] == 'indeterminate'
    assert cert['canonical_entities'] == []
    assert 'optimality' not in cert
    bounds = cert['bounds']
    # 下界是真实可行的贪心见证（直连 25/5），上界是每左记录最大边分之和 10+10+8
    assert bounds['total_score'] == {'lower': 25, 'upper': 28}
    assert bounds['minimum_score'] == {'lower': 5, 'upper': 8}
    witness = cert['feasible_witness']
    assert witness['optimal'] is False
    assert witness['total_score'] == 25
    result = verify_certificate(db, cert['certificate_id'])
    assert result['ok'], result['problems']


def test_tie_break_follows_frozen_id_order(tmp_path):
    db = _db(tmp_path)
    doc = {'manifest_id': 'link-T1', 'batch_id': 'link-demo', 'dataset_version': 'd2',
           'left_provider': 'A', 'right_provider': 'B',
           'left_ids': ['a1', 'a2'], 'right_ids': ['b1', 'b2'],
           'edges': [{'left': l, 'right': r,
                      'claims': [{'id': f't{i}{j}a', 'source': 'matcher-A', 'score': 5},
                                 {'id': f't{i}{j}b', 'source': 'matcher-B', 'score': 5}]}
                     for i, l in enumerate(('a1', 'a2')) for j, r in enumerate(('b1', 'b2'))]}
    record(db, _write(tmp_path, doc, 'tie.json'))
    cert = decide(db, _write(tmp_path, {'certificate_id': 'link-cert-tie',
                                        'manifest_id': 'link-T1',
                                        'contract': 'max_total_score'}, 'tie-plan.json'))
    # 两种完整匹配目标值相同，按冻结 ID 序取直连
    assert _pairs(cert) == [('a1', 'b1'), ('a2', 'b2')]
    assert (cert['total_score'], cert['minimum_score']) == (10, 5)


def test_unknown_certificate_and_bad_plan(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValueError, match='link_unknown_certificate'):
        verify_certificate(db, 'nope')
    with pytest.raises(ValueError, match='link_unknown_manifest'):
        decide(db, _write(tmp_path, {'certificate_id': 'c1', 'manifest_id': 'nope',
                                     'contract': 'max_total_score'}, 'p1.json'))
    with pytest.raises(ValueError, match='link_bad_plan'):
        decide(db, _write(tmp_path, {'certificate_id': 'c2', 'manifest_id': 'link-S1',
                                     'contract': 'cheat'}, 'p2.json'))
    with pytest.raises(ValueError, match='link_bad_budget'):
        decide(db, _write(tmp_path, {'certificate_id': 'c3', 'manifest_id': 'link-S1',
                                     'contract': 'max_total_score', 'budget': 0}, 'p3.json'))
