"""Offline end-to-end demonstration of correction restatement.

Everything runs against local SQLite and the synthetic fixtures shipped in
``fixtures/``; no network or external service is touched.
"""
import json
import sqlite3
import tempfile
from pathlib import Path

from .cohort import (
    apply_plan, build_plan, plan_issues, query_metric, record_corrections,
    record_snapshot,
)
from .ingest import initialize, load
from .report import export, verify
from .rules import evaluate

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures'
SNAPSHOT_A = FIXTURE / 'cohort-A-snapshot.json'
SNAPSHOT_B = FIXTURE / 'cohort-B-snapshot.json'
CORRECTION_A = FIXTURE / 'cohort-A-corrections.jsonl'
CORRECTION_B = FIXTURE / 'cohort-B-corrections.jsonl'
CONFIG = FIXTURE / 'cohort-config.json'
GRANTS = FIXTURE / 'cohort-grants.jsonl'
PRE_QUERY = '2026-06-01T00:00:00Z'
POST_QUERY = '2026-07-01T00:00:00Z'


def _bootstrap(db):
    initialize(db, CONFIG)
    load(db, GRANTS)
    record_snapshot(db, SNAPSHOT_A)
    record_snapshot(db, SNAPSHOT_B)
    record_corrections(db, CORRECTION_A)
    record_corrections(db, CORRECTION_B)
    evaluate(db)
    # Freeze the plan against this completed authorization run; later runs and
    # new product versions must reference it rather than rewrite its decisions.
    run_id = sqlite3.connect(db).execute('SELECT max(id) FROM run').fetchone()[0]
    return run_id


def _contract_database(work, contract):
    db = work / f'{contract}.db'
    run_id = _bootstrap(db)
    plan = build_plan(db, contract, run_id=run_id)
    applied = apply_plan(db, plan['plan_id'])
    return db, plan, applied, run_id


def run_demo(workdir=None):
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix='cohort-demo-'))
    work.mkdir(parents=True, exist_ok=True)
    summary = {'workdir': str(work), 'contracts': {}, 'acceptance': {}, 'conflicts': {}}

    for contract in ('retroactive_restate', 'prospective_version'):
        db, plan, applied, run_id = _contract_database(work, contract)
        pre_rank = query_metric(db, 'cohort-A', 'p2', 'cohort_rank', PRE_QUERY)
        pre_mean = query_metric(db, 'cohort-A', 'p2', 'mean', PRE_QUERY)
        post_rank = query_metric(db, 'cohort-A', 'p2', 'cohort_rank', POST_QUERY)
        post_mean = query_metric(db, 'cohort-A', 'p2', 'mean', POST_QUERY)
        # p1 drives both cohorts but each recomputes its own closure.
        closure_b = query_metric(db, 'cohort-B', 'p3', 'cohort_rank', POST_QUERY)
        closure_b_mean = query_metric(db, 'cohort-B', 'p1', 'mean', POST_QUERY)
        decisions = evaluate(db)
        report_path = export(db, work / f'{contract}-report.json')
        problems = verify(db)
        summary['contracts'][contract] = {
            'plan_id': plan['plan_id'],
            'bound_authorization_run': run_id,
            'versions': applied['versions'],
            'pre_correction_query': {'p2_cohort_rank': pre_rank['value'],
                                     'cohort_mean': pre_mean['value'],
                                     'version': pre_rank['version_id'],
                                     'version_status': pre_rank['version_status']},
            'post_correction_query': {'p2_cohort_rank': post_rank['value'],
                                      'cohort_mean': post_mean['value'],
                                      'version': post_rank['version_id'],
                                      'version_status': post_rank['version_status']},
            'cohort_B_closure': {'p3_rank': closure_b['value'],
                                 'mean': closure_b_mean['value'],
                                 'version': closure_b['version_id']},
            'decisions': decisions,
            'report': str(report_path),
            'verify_problems': problems,
        }

    # Contributor gate: p2's own licence cannot cover p1's contribution.
    db_gap = work / 'auth-gap.db'
    gap_run = _bootstrap(db_gap)
    gap_plan = build_plan(db_gap, 'prospective_version', run_id=gap_run)
    apply_plan(db_gap, gap_plan['plan_id'])
    gap_decisions = evaluate(db_gap)
    gap_detail = json.loads(sqlite3.connect(db_gap).execute(
        "SELECT detail FROM decision WHERE subject='req-p2-only' "
        "ORDER BY id DESC LIMIT 1").fetchone()[0])
    summary['acceptance']['contributor_gate'] = {
        'req-cohort': gap_decisions['req-cohort'],
        'req-p2-only': gap_decisions['req-p2-only'],
        'blocking_issue': gap_detail['issues'],
    }

    # Conflict ledger: bad old value + mutually exclusive same-instant fixes.
    db_conflict = work / 'conflict.db'
    initialize(db_conflict, CONFIG)
    load(db_conflict, GRANTS)
    record_snapshot(db_conflict, SNAPSHOT_A)
    bad = work / 'bad-corrections.jsonl'
    bad.write_text('\n'.join((
        json.dumps({'event_key': 'bad-old', 'cohort_id': 'cohort-A', 'person': 'p1',
                    'field': 'score', 'old_value': '10.00', 'new_value': '90.00',
                    'at': '2026-06-02T09:00:00Z'}),
        json.dumps({'event_key': 'exclusive-1', 'cohort_id': 'cohort-A', 'person': 'p2',
                    'field': 'score', 'old_value': '80.00', 'new_value': '77.00',
                    'at': '2026-06-03T08:00:00Z'}),
        json.dumps({'event_key': 'exclusive-2', 'cohort_id': 'cohort-A', 'person': 'p2',
                    'field': 'score', 'old_value': '80.00', 'new_value': '55.00',
                    'at': '2026-06-03T08:00:00Z'}),
    )), encoding='utf-8')
    record_corrections(db_conflict, bad)
    evaluate(db_conflict)
    conflict_run = sqlite3.connect(db_conflict).execute(
        'SELECT max(id) FROM run').fetchone()[0]
    blocked_plan = build_plan(db_conflict, 'retroactive_restate', run_id=conflict_run)
    apply_plan(db_conflict, blocked_plan['plan_id'])
    issues = plan_issues(db_conflict, blocked_plan['plan_id'])
    versions = sqlite3.connect(db_conflict).execute(
        'SELECT id,status FROM cohort_version ORDER BY seq').fetchall()
    summary['conflicts'] = {
        'plan_id': blocked_plan['plan_id'],
        'blocked_cohorts': blocked_plan['blocked_cohorts'],
        'issue_codes': sorted({i['code'] for i in issues}),
        'published_versions': versions,
        'verify_problems': verify(db_conflict),
    }
    summary['acceptance']['exact_values'] = {
        'baseline_mean': '70.00', 'restated_mean': '85.00',
        'p2_baseline_rank': 1, 'p2_restated_rank': 2,
    }
    return summary
