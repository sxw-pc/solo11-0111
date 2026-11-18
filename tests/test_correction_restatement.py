import json
import sqlite3
from pathlib import Path

import pytest

from purposegate.cohort import (
    CohortError, apply_plan, build_plan, compute_metrics, plan_issues,
    query_metric, record_corrections, record_snapshot,
)
from purposegate.ingest import initialize, load
from purposegate.report import export, verify
from purposegate.rules import evaluate
from purposegate.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'
SNAP_A = FIX / 'cohort-A-snapshot.json'
SNAP_B = FIX / 'cohort-B-snapshot.json'
FIX_A = FIX / 'cohort-A-corrections.jsonl'
FIX_B = FIX / 'cohort-B-corrections.jsonl'
CONFIG = FIX / 'cohort-config.json'
GRANTS = FIX / 'cohort-grants.jsonl'
PRE = '2026-06-01T00:00:00Z'
POST = '2026-07-01T00:00:00Z'
REGISTRATION = '2026-06-02T09:00:00+00:00'


def bootstrap(db, snapshots=(SNAP_A, SNAP_B), corrections=(FIX_A, FIX_B),
              config=CONFIG, grants=GRANTS):
    if config is not None:
        initialize(db, config)
        if grants is not None:
            load(db, grants)
    for snap in snapshots:
        record_snapshot(db, snap)
    for corr in corrections:
        record_corrections(db, corr)
    return db


def apply(db, contract):
    plan = build_plan(db, contract)
    applied = apply_plan(db, plan['plan_id'])
    return plan, applied


# --------------------------------------------------------------------------
# Metric arithmetic is fixed and replayable
# --------------------------------------------------------------------------

def test_mean_and_dense_rank_arithmetic():
    mean, ranks = compute_metrics({'p1': '60.00', 'p2': '80.00'})
    assert (mean, ranks) == ('70.00', {'p1': 2, 'p2': 1})
    mean2, ranks2 = compute_metrics({'p1': '90.00', 'p2': '80.00'})
    assert (mean2, ranks2) == ('85.00', {'p1': 1, 'p2': 2})
    # ties share a dense rank and the next distinct score advances by one.
    mean3, ranks3 = compute_metrics({'a': '10.00', 'b': '10.00', 'c': '5.00'})
    assert mean3 == '8.33'
    assert ranks3 == {'a': 1, 'b': 1, 'c': 2}


# --------------------------------------------------------------------------
# 1. Cross-cohort impact closure with exact values
# --------------------------------------------------------------------------

def test_correction_restates_whole_cohort_not_only_p1_row(tmp_path):
    db = bootstrap(tmp_path / 'c.db')
    plan, applied = apply(db, 'retroactive_restate')
    con = sqlite3.connect(db)
    v2 = json.loads(con.execute(
        "SELECT values_json FROM cohort_version WHERE id='cohort-A@v2'").fetchone()[0])
    con.close()
    # mean moves 70.00 -> 85.00 and untouched p2 loses rank 1.
    assert v2['mean'] == '85.00'
    assert v2['ranks'] == {'p1': 1, 'p2': 2}
    assert v2['scores'] == {'p1': '90.00', 'p2': '80.00'}
    p2 = query_metric(db, 'cohort-A', 'p2', 'cohort_rank', POST)
    assert (p2['value'], p2['version_id']) == (2, 'cohort-A@v2')
    assert plan['impact']['cohort-A']['corrected_persons'] == ['p1']


def test_shared_p1_touches_each_cohort_independently(tmp_path):
    db = bootstrap(tmp_path / 'c.db')
    apply(db, 'prospective_version')
    # cohort-B recomputes from its own members p1/p3; ranks never copied from A.
    rank_p3 = query_metric(db, 'cohort-B', 'p3', 'cohort_rank', POST)
    mean_b = query_metric(db, 'cohort-B', 'p1', 'mean', POST)
    assert rank_p3['value'] == 2
    assert mean_b['value'] == '80.00'
    con = sqlite3.connect(db)
    rows = con.execute(
        'SELECT cohort_id,id,status FROM cohort_version ORDER BY cohort_id,seq').fetchall()
    con.close()
    assert rows == [
        ('cohort-A', 'cohort-A@v1', 'archived'),
        ('cohort-A', 'cohort-A@v2', 'current'),
        ('cohort-B', 'cohort-B@v1', 'archived'),
        ('cohort-B', 'cohort-B@v2', 'current')]


def test_version_binds_real_contributor_set_not_field_names(tmp_path):
    db = bootstrap(tmp_path / 'c.db')
    _, applied = apply(db, 'retroactive_restate')
    con = sqlite3.connect(db)
    members = json.loads(con.execute(
        "SELECT member_set_json FROM cohort_version WHERE id='cohort-A@v2'").fetchone()[0])
    auth = json.loads(con.execute(
        "SELECT auth_summary_json FROM cohort_version WHERE id='cohort-A@v2'").fetchone()[0])
    con.close()
    assert {m['person'] for m in members} == {'p1', 'p2'}
    assert {c['person'] for c in auth['contributors']} == {'p1', 'p2'}
    assert all(c['source_field'] == 'score' for c in auth['contributors'])
    # the frozen summary references real authorization events per contributor
    keys = {c['person']: c['evidence_event_keys'] for c in auth['contributors']}
    assert keys['p1'] == ['cg-p1']
    assert set(keys['p2']) == {'cg-p2', 'cg-p2-only'}
    assert auth['subjects'] == ['req-cohort', 'req-p2-only']


# --------------------------------------------------------------------------
# 2. The two frozen historical contracts answer pre/post queries differently
# --------------------------------------------------------------------------

def test_retroactive_contract_restates_pre_correction_query(tmp_path):
    db = bootstrap(tmp_path / 'c.db')
    apply(db, 'retroactive_restate')
    rank = query_metric(db, 'cohort-A', 'p2', 'cohort_rank', PRE)
    mean = query_metric(db, 'cohort-A', 'p2', 'mean', PRE)
    assert (rank['value'], mean['value']) == (2, '85.00')
    assert rank['version_id'] == 'cohort-A@v2'
    con = sqlite3.connect(db)
    assert con.execute(
        "SELECT status FROM cohort_version WHERE id='cohort-A@v1'").fetchone()[0] \
        == 'superseded'
    con.close()


def test_prospective_contract_keeps_old_authority_before_registration(tmp_path):
    db = bootstrap(tmp_path / 'c.db')
    apply(db, 'prospective_version')
    pre_rank = query_metric(db, 'cohort-A', 'p2', 'cohort_rank', PRE)
    pre_mean = query_metric(db, 'cohort-A', 'p2', 'mean', PRE)
    assert (pre_rank['value'], pre_mean['value']) == (1, '70.00')
    assert pre_rank['version_id'] == 'cohort-A@v1'
    post_rank = query_metric(db, 'cohort-A', 'p2', 'cohort_rank', POST)
    post_mean = query_metric(db, 'cohort-A', 'p2', 'mean', POST)
    assert (post_rank['value'], post_mean['value']) == (2, '85.00')
    assert post_rank['version_id'] == 'cohort-A@v2'


def test_contract_cannot_be_switched_per_query(tmp_path):
    db = bootstrap(tmp_path / 'c.db')
    apply(db, 'prospective_version')
    with pytest.raises(CohortError, match='contract_mismatch'):
        query_metric(db, 'cohort-A', 'p2', 'cohort_rank', PRE,
                     contract='retroactive_restate')


def test_superseded_version_cannot_impersonate_current(tmp_path):
    db = bootstrap(tmp_path / 'c.db')
    apply(db, 'retroactive_restate')
    con = sqlite3.connect(db)
    assert con.execute(
        "SELECT count(*) FROM cohort_version WHERE cohort_id='cohort-A' AND status='current'"
    ).fetchone()[0] == 1
    con.close()
    # a second, different contract for the same cohort is rejected rather than
    # silently changing history.
    with pytest.raises(CohortError, match='contract_frozen'):
        plan2 = build_plan(db, 'prospective_version')
        apply_plan(db, plan2['plan_id'])


# --------------------------------------------------------------------------
# 3. Missing contributor authorization blocks derived output
# --------------------------------------------------------------------------

def _decision_detail(db, subject):
    row = connect(db).execute(
        'SELECT detail FROM decision WHERE subject=? ORDER BY id DESC LIMIT 1',
        (subject,)).fetchone()
    return json.loads(row[0])


def test_contributor_authorization_gate(tmp_path):
    db = bootstrap(tmp_path / 'c.db')
    apply(db, 'prospective_version')
    decisions = evaluate(db)
    assert decisions['req-cohort'] == '允许'
    # req-p2-only has a licence for p2 only; it cannot cover p1's contribution.
    assert decisions['req-p2-only'] == '无法判定'
    detail = _decision_detail(db, 'req-p2-only')
    assert 'contributor_evidence_gap' in detail['issues']
    gate = detail['cohort_derived']
    assert gate['contributors'] == ['p1', 'p2']
    assert gate['contributor_partitions']['p1']['status'] == '拒绝'
    assert gate['contributor_partitions']['p2']['status'] == '允许'
    assert 'cohort_rank' not in detail['allowed_fields']


def test_derived_request_without_applied_version_is_undecidable(tmp_path):
    db = bootstrap(tmp_path / 'c.db')
    assert evaluate(db)['req-cohort'] == '无法判定'
    detail = _decision_detail(db, 'req-cohort')
    assert 'cohort_version_missing' in detail['issues']


def test_revoked_contributor_blocks_even_if_old_score_was_licensed(tmp_path):
    db = bootstrap(tmp_path / 'c.db')
    apply(db, 'prospective_version')
    revoke = tmp_path / 'revoke.jsonl'
    revoke.write_text(json.dumps({
        'event_key': 'revoke-p1', 'subject': 'req-cohort', 'kind': 'revoke',
        'person': 'p1', 'at': '2026-06-20T00:00:00Z'}), encoding='utf-8')
    load(db, revoke)
    assert evaluate(db)['req-cohort'] == '无法判定'
    detail = _decision_detail(db, 'req-cohort')
    assert 'contributor_evidence_gap' in detail['issues']


# --------------------------------------------------------------------------
# 4. Conflicts / duplicates / drift stay traceable and never publish values
# --------------------------------------------------------------------------

def test_bad_old_value_and_mutex_corrections_block_publication(tmp_path):
    db = tmp_path / 'conflict.db'
    initialize(db, CONFIG)
    load(db, GRANTS)
    record_snapshot(db, SNAP_A)
    bad = tmp_path / 'bad.jsonl'
    bad.write_text('\n'.join((
        json.dumps({'event_key': 'bad-old', 'cohort_id': 'cohort-A', 'person': 'p1',
                    'field': 'score', 'old_value': '10.00', 'new_value': '90.00',
                    'at': '2026-06-02T09:00:00Z'}),
        json.dumps({'event_key': 'mutex-1', 'cohort_id': 'cohort-A', 'person': 'p2',
                    'field': 'score', 'old_value': '80.00', 'new_value': '77.00',
                    'at': '2026-06-03T08:00:00Z'}),
        json.dumps({'event_key': 'mutex-2', 'cohort_id': 'cohort-A', 'person': 'p2',
                    'field': 'score', 'old_value': '80.00', 'new_value': '55.00',
                    'at': '2026-06-03T08:00:00Z'}),
    )), encoding='utf-8')
    record_corrections(db, bad)
    plan = build_plan(db, 'retroactive_restate')
    assert plan['blocked_cohorts'] == ['cohort-A']
    apply_plan(db, plan['plan_id'])
    codes = {i['code'] for i in plan_issues(db, plan['plan_id'])}
    assert {'old_value_mismatch', 'mutually_exclusive_corrections'} <= codes
    con = sqlite3.connect(db)
    assert con.execute(
        "SELECT count(*) FROM cohort_version WHERE cohort_id='cohort-A'").fetchone()[0] == 1
    con.close()
    with pytest.raises(CohortError, match='cohort_correction_blocked'):
        query_metric(db, 'cohort-A', 'p2', 'cohort_rank', POST)


def test_correction_for_unknown_member_is_blocked(tmp_path):
    db = tmp_path / 'm.db'
    initialize(db, CONFIG)
    load(db, GRANTS)
    record_snapshot(db, SNAP_A)
    bad = tmp_path / 'ghost.jsonl'
    bad.write_text(json.dumps({
        'event_key': 'ghost', 'cohort_id': 'cohort-A', 'person': 'p9',
        'field': 'score', 'old_value': '1.00', 'new_value': '2.00',
        'at': '2026-06-02T09:00:00Z'}), encoding='utf-8')
    record_corrections(db, bad)
    plan = build_plan(db, 'prospective_version')
    assert plan['blocked_cohorts'] == ['cohort-A']
    assert any(i['code'] == 'correction_member_missing'
               for i in plan_issues(db, plan['plan_id']))


def test_chained_corrections_must_follow_old_value_order(tmp_path):
    db = tmp_path / 'chain.db'
    record_snapshot(db, SNAP_A)
    chain = tmp_path / 'chain.jsonl'
    chain.write_text('\n'.join((
        json.dumps({'event_key': 'k1', 'cohort_id': 'cohort-A', 'person': 'p1',
                    'field': 'score', 'old_value': '60.00', 'new_value': '65.00',
                    'at': '2026-06-02T09:00:00Z'}),
        json.dumps({'event_key': 'k2', 'cohort_id': 'cohort-A', 'person': 'p1',
                    'field': 'score', 'old_value': '65.00', 'new_value': '90.00',
                    'at': '2026-06-03T09:00:00Z'}),
    )), encoding='utf-8')
    record_corrections(db, chain)
    _apply_two(db)
    v2 = query_metric(db, 'cohort-A', 'p1', 'cohort_rank', POST)
    assert v2['value'] == 1
    assert query_metric(db, 'cohort-A', 'p2', 'mean', POST)['value'] == '85.00'


def _apply_two(db):
    plan = build_plan(db, 'retroactive_restate')
    return plan, apply_plan(db, plan['plan_id'])


def test_stored_drift_is_reported_with_cohort_member_event(tmp_path):
    db = bootstrap(tmp_path / 'd.db')
    plan, _ = apply(db, 'prospective_version')
    con = sqlite3.connect(db)
    con.execute("UPDATE cohort_version SET digest='00' WHERE id='cohort-A@v2'")
    con.commit()
    con.close()
    problems = verify(db)
    assert any(p == 'cohort_digest_drift:cohort-A:cohort-A@v2' for p in problems)
    con = sqlite3.connect(db)
    con.execute("UPDATE cohort_member SET score='70.00' WHERE cohort_id='cohort-A' "
                "AND person='p1'")
    con.commit()
    con.close()
    problems = verify(db)
    assert any(p.startswith('cohort_evidence:snapshot_drift:cohort-A') for p in problems)


# --------------------------------------------------------------------------
# 5. Independent verification and repeated runs are stable
# --------------------------------------------------------------------------

def test_verify_clean_and_plan_apply_are_idempotent(tmp_path):
    db = bootstrap(tmp_path / 'i.db')
    first = build_plan(db, 'retroactive_restate')
    again = build_plan(db, 'retroactive_restate')
    assert first['plan_id'] == again['plan_id'] and again['idempotent'] is True
    applied1 = apply_plan(db, first['plan_id'])
    applied2 = apply_plan(db, first['plan_id'])
    assert applied2['idempotent'] is True
    assert applied1['versions'] == applied2['versions']
    assert verify(db) == []
    # evaluation reruns deterministically and stays independently recheckable
    assert evaluate(db) == evaluate(db)


def test_recompute_detects_mean_and_rank_tamper(tmp_path):
    db = bootstrap(tmp_path / 't.db')
    apply(db, 'retroactive_restate')
    con = sqlite3.connect(db)
    raw = con.execute(
        "SELECT values_json FROM cohort_version WHERE id='cohort-A@v2'").fetchone()[0]
    tampered = json.loads(raw)
    tampered['mean'] = '99.99'
    con.execute("UPDATE cohort_version SET values_json=? WHERE id='cohort-A@v2'",
                (json.dumps(tampered, sort_keys=True),))
    con.commit()
    con.close()
    problems = verify(db)
    assert any(p.startswith('cohort_mean_drift:cohort-A:cohort-A@v2') for p in problems)


def test_export_contains_frozen_chain_and_report_verifies(tmp_path):
    db = bootstrap(tmp_path / 'r.db')
    apply(db, 'retroactive_restate')
    evaluate(db)
    out = export(db, tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    assert {v['id'] for v in data['cohort_versions']} >= {
        'cohort-A@v1', 'cohort-A@v2', 'cohort-B@v1', 'cohort-B@v2'}
    assert data['correction_plans'][0]['status'] == 'applied'
    assert verify(db) == []


def test_decision_run_and_original_delivery_bytes_are_not_rewritten(tmp_path):
    # legacy intake evidence tables remain untouched by the new pipeline.
    db = bootstrap(tmp_path / 'f.db')
    before = [tuple(r) for r in connect(db).execute(
        'SELECT event_key,old_value,new_value FROM cohort_correction ORDER BY event_key'
    ).fetchall()]
    apply(db, 'prospective_version')
    after = [tuple(r) for r in connect(db).execute(
        'SELECT event_key,old_value,new_value FROM cohort_correction ORDER BY event_key'
    ).fetchall()]
    assert before == after == [('cohort-B-fix-1', '60.00', '90.00'),
                               ('cohort-fix-1', '60.00', '90.00')]


def test_corrupt_source_digest_blocks_cohort_and_is_traceable(tmp_path):
    db = bootstrap(tmp_path / 'corrupt.db')
    con = sqlite3.connect(db)
    for row in con.execute("SELECT id FROM cohort_source WHERE kind='snapshot'"):
        con.execute("UPDATE cohort_source SET digest=? WHERE id=?",
                    (f'bad-{row[0]}', row[0]))
    con.commit()
    con.close()
    plan = build_plan(db, 'prospective_version')
    assert plan['blocked_cohorts'] == ['cohort-A', 'cohort-B']
    codes = {i['code'] for i in plan_issues(db, plan['plan_id'])}
    assert 'source_digest_corrupt' in codes
    apply_plan(db, plan['plan_id'])
    # only baselines exist; no corrected product is published
    con = sqlite3.connect(db)
    assert con.execute('SELECT count(*) FROM cohort_version WHERE seq=2').fetchone()[0] == 0
    con.close()
    assert any('source_digest_corrupt' in p for p in verify(db))


def test_metric_definition_missing_blocks_version(tmp_path):
    db = tmp_path / 'no-metric.db'
    snap = tmp_path / 'snap.json'
    snap.write_text(json.dumps({
        'cohort_id': 'cohort-X', 'dataset_version': 'd1',
        'metric_contract': {'mean': 'something_else'},
        'members': [{'person': 'p1', 'score': '1.00'}]}), encoding='utf-8')
    record_snapshot(db, snap)
    plan = build_plan(db, 'retroactive_restate')
    assert plan['blocked_cohorts'] == ['cohort-X']
    apply_plan(db, plan['plan_id'])
    con = sqlite3.connect(db)
    assert con.execute(
        "SELECT count(*) FROM cohort_version WHERE cohort_id='cohort-X' AND seq=2"
    ).fetchone()[0] == 0
    con.close()
