import json
from decimal import Decimal
from pathlib import Path

from .store import connect
from .cohort import (
    MEAN_METRIC, RANK_METRIC, SCORE_FIELD, compute_metrics,
    digest_of, migrate as cohort_migrate,
)
from .ingest import hash_bytes
from .timeline import instant


def export(db_path, out):
    db = connect(db_path)
    cohort_migrate(db)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    run = db.execute('SELECT * FROM run ORDER BY id DESC LIMIT 1').fetchone()
    if not run:
        raise ValueError('no_run')
    rid = run['id']
    data = {'run': dict(run),
            'sources': [{'id': x['id'], 'name': x['name'], 'digest': x['digest']}
                        for x in db.execute('SELECT * FROM source_file ORDER BY id')],
            'decisions': [dict(x) for x in db.execute(
                'SELECT * FROM decision WHERE run_id=? ORDER BY subject', (rid,))],
            'issues': [dict(x) for x in db.execute(
                'SELECT * FROM issue WHERE run_id=? OR run_id IS NULL ORDER BY id', (rid,))],
            'evidence': [dict(x) for x in db.execute(
                'SELECT * FROM evidence WHERE run_id=? ORDER BY id', (rid,))]}
    data['executions'] = [dict(x) for x in db.execute('SELECT * FROM execution ORDER BY id')]
    data['correction_plans'] = [dict(x) for x in db.execute(
        'SELECT id,contract,status,run_id,input_digest,impact_json,created_at,applied_at '
        'FROM correction_plan ORDER BY id')]
    data['cohort_versions'] = [dict(x) for x in db.execute(
        'SELECT id,cohort_id,seq,status,contract,effective_date,effective_from,'
        'superseded_at,predecessor_id,plan_id,values_json,digest '
        'FROM cohort_version ORDER BY cohort_id,seq')]
    data['plan_issues'] = [dict(x) for x in db.execute(
        'SELECT plan_id,cohort_id,person,event_key,code,detail FROM plan_issue ORDER BY id')]
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    md = out.with_suffix('.md')
    md.write_text('# 离线判定报告\n\n'
                  + f'运行 {rid}：{run["status"]}\n\n'
                  + '\n'.join(f'- {x["subject"]}: {x["status"]}，{x["detail"]}'
                              for x in data['decisions'])
                  + '\n\n## Cohort 版本\n\n'
                  + '\n'.join(f'- {v["cohort_id"]} {v["id"]} ({v["status"]}, '
                              f'{v["contract"] or "-"})' for v in data['cohort_versions']),
                  encoding='utf-8')
    db.close()
    return out


def _recompute_version(db, version):
    """Independently replay snapshot bytes + correction text into metrics."""
    source = db.execute('SELECT raw FROM cohort_source WHERE id=?',
                        (version['source_snapshot_id'],)).fetchone()
    if source is None:
        return None, f'snapshot_source_missing:{version["cohort_id"]}'
    snapshot = json.loads(source['raw'])
    scores = {m['person']: str(Decimal(str(m['score']))) for m in snapshot['members']}
    for event_key in json.loads(version['corrections_json']):
        corr = db.execute('SELECT * FROM cohort_correction WHERE event_key=?',
                          (event_key,)).fetchone()
        if corr is None:
            return None, f'correction_missing:{version["cohort_id"]}:{event_key}'
        lines = db.execute('SELECT raw FROM cohort_source WHERE id=?',
                           (corr['source_id'],)).fetchone()['raw'].splitlines()
        try:
            line = lines[corr['line_no'] - 1]
        except IndexError:
            return None, f'correction_line_missing:{event_key}'
        if hash_bytes(line if isinstance(line, bytes) else line.encode('utf-8')) \
                != corr['digest']:
            return None, f'correction_drift:{version["cohort_id"]}:{event_key}'
        if scores.get(corr['person']) != corr['old_value']:
            return None, f'correction_chain_drift:{version["cohort_id"]}:{event_key}'
        scores[corr['person']] = corr['new_value']
    mean, ranks = compute_metrics(scores)
    return {'mean': mean, 'ranks': ranks, 'scores': scores}, None


def _verify_cohorts(db):
    problems = []
    from .cohort import verify_cohort_evidence
    for code, cohort_id, person, event_key in verify_cohort_evidence(db):
        problems.append(f'cohort_evidence:{code}:{cohort_id}:'
                        f'{person or "-"}:{event_key or "-"}')
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cohort_version'") \
            .fetchone():
        return problems
    plans = {p['id']: dict(p) for p in db.execute('SELECT * FROM correction_plan')}
    versions = [dict(v) for v in db.execute('SELECT * FROM cohort_version ORDER BY cohort_id,seq')]
    known_event_keys = {r['event_key'] for r in db.execute('SELECT event_key FROM raw_event')}
    for version in versions:
        cid = version['cohort_id']
        recomputed, error = _recompute_version(db, version)
        if error:
            problems.append(f'cohort_version:{cid}:{version["id"]}:{error}')
            continue
        stored = json.loads(version['values_json'])
        if recomputed['mean'] != stored['mean']:
            problems.append(f'cohort_mean_drift:{cid}:{version["id"]}:'
                            f'{stored["mean"]}!={recomputed["mean"]}')
        if recomputed['ranks'] != stored['ranks']:
            problems.append(f'cohort_rank_drift:{cid}:{version["id"]}')
        if recomputed['scores'] != stored['scores']:
            problems.append(f'cohort_score_drift:{cid}:{version["id"]}')
        members = json.loads(version['member_set_json'])
        if sorted(m['person'] for m in members) != sorted(recomputed['scores']):
            problems.append(f'cohort_contributor_drift:{cid}:{version["id"]}')
        contract = None
        if version['plan_id'] in plans:
            plan_row = db.execute('SELECT body_json FROM correction_plan WHERE id=?',
                                  (version['plan_id'],)).fetchone()
            if plan_row is not None:
                contract = json.loads(plan_row['body_json'])['contract']
        if contract != version['contract']:
            problems.append(f'cohort_contract_drift:{cid}:{version["id"]}')
        metric_contract = json.loads(version['metric_contract_json'])
        if metric_contract.get('mean') != MEAN_METRIC or \
                metric_contract.get('rank') != RANK_METRIC:
            problems.append(f'cohort_metric_contract:{cid}:{version["id"]}')
        snap_row = db.execute(
            'SELECT raw_digest FROM cohort_snapshot WHERE cohort_id=?', (cid,)).fetchone()
        if snap_row is None:
            problems.append(f'cohort_snapshot_missing:{cid}:{version["id"]}')
            continue
        # Rebuild the version digest from the same canonical payload inputs.
        rebuilt_digest = digest_of({
            'cohort_id': cid, 'seq': version['seq'], 'members': members,
            'metric_contract': metric_contract, 'values': stored,
            'corrections': json.loads(version['corrections_json']),
            'snapshot_digest': snap_row['raw_digest'],
            **({'plan_id': version['plan_id']} if version['seq'] > 1 else {})})
        if rebuilt_digest != version['digest']:
            problems.append(f'cohort_digest_drift:{cid}:{version["id"]}')
        auth = json.loads(version['auth_summary_json'])
        if auth.get('run_id') != version['auth_run_id']:
            problems.append(f'cohort_auth_run_drift:{cid}:{version["id"]}')
        contributor_names = {m['person'] for m in members}
        auth_names = {c['person'] for c in auth.get('contributors', [])}
        if contributor_names != auth_names:
            problems.append(f'cohort_auth_contributor_drift:{cid}:{version["id"]}')
        for contributor in auth.get('contributors', []):
            if contributor.get('source_field') != SCORE_FIELD:
                problems.append(f'cohort_auth_field_drift:{cid}:'
                                f'{contributor.get("person")}')
            for event_key in contributor.get('evidence_event_keys', []):
                if event_key not in known_event_keys:
                    problems.append(f'cohort_auth_event_missing:{cid}:'
                                    f'{contributor["person"]}:{event_key}')
        # State-machine checks for the two frozen historical contracts.
        same_cohort = [v for v in versions if v['cohort_id'] == cid]
        if len(same_cohort) > 1:
            baseline, current = same_cohort[0], same_cohort[-1]
            correction_rows = [dict(r) for r in db.execute(
                'SELECT * FROM cohort_correction WHERE cohort_id=? ORDER BY at,event_key', (cid,))]
            registration = max(instant(c['at']) for c in correction_rows).isoformat()
            if version['contract'] == 'retroactive_restate':
                if baseline['status'] != 'superseded':
                    problems.append(f'cohort_state_drift:{cid}:baseline_not_superseded')
                if current['status'] != 'current' or current['effective_from'] != \
                        baseline['effective_date']:
                    problems.append(f'cohort_state_drift:{cid}:retro_effectivity')
            elif version['contract'] == 'prospective_version':
                if baseline['status'] != 'archived' or \
                        baseline['superseded_at'] != registration:
                    problems.append(f'cohort_state_drift:{cid}:baseline_not_archived')
                if current['status'] != 'current' or \
                        current['effective_from'] != registration:
                    problems.append(f'cohort_state_drift:{cid}:prospective_effectivity')
            if any(s['status'] == 'current' for s in same_cohort) is False:
                problems.append(f'cohort_state_drift:{cid}:no_current_version')
            if current['predecessor_id'] != baseline['id']:
                problems.append(f'cohort_predecessor_drift:{cid}')
    # A blocked cohort must carry traceable plan issues and no v2 product.
    for plan_id, plan in plans.items():
        body = json.loads(plan['body_json'])
        issue_codes = {r['code'] for r in db.execute(
            'SELECT code FROM plan_issue WHERE plan_id=?', (plan_id,))}
        for cid, info in body['cohorts'].items():
            has_v2 = db.execute('SELECT 1 FROM cohort_version WHERE cohort_id=? AND seq=2',
                                (cid,)).fetchone()
            if info['blocked'] and has_v2:
                problems.append(f'cohort_blocked_published:{cid}')
            # Only applied plans promise concrete version products; a plan still
            # in 'planned' legitimately has no v2 rows yet.
            if plan['status'] == 'applied' and not info['blocked'] \
                    and info['corrections'] and not has_v2:
                problems.append(f'cohort_version_missing:{cid}:v2')
            if info['blocked'] and not issue_codes:
                problems.append(f'cohort_blocked_without_issue:{cid}')
    return problems


def verify(db_path):
    db = connect(db_path)
    cohort_migrate(db)
    problems = []
    for batch in db.execute('SELECT id,config_raw,config_hash FROM batch'):
        if hash_bytes(batch['config_raw']) != batch['config_hash']:
            problems.append(f'batch_config:{batch["id"]}')
    sources = {s['id']: dict(s) for s in db.execute('SELECT * FROM source_file')}
    for s in sources.values():
        if hash_bytes(s['raw']) != s['digest']:
            problems.append(f'source:{s["id"]}')
    events = {e['id']: dict(e) for e in db.execute('SELECT * FROM raw_event')}
    for e in events.values():
        try:
            lines = sources[e['source_id']]['raw'].decode('utf-8-sig').splitlines()
            original = lines[e['line_no'] - 1]
            if hash_bytes(original.encode('utf-8')) != e['digest'] or \
                    json.loads(original) != json.loads(e['payload']):
                problems.append(f'event:{e["id"]}')
        except (KeyError, IndexError, UnicodeError, ValueError):
            problems.append(f'event:{e["id"]}')
    for ev in db.execute('SELECT * FROM evidence WHERE event_id IS NOT NULL'):
        source_event = events.get(ev['event_id'])
        if not source_event or source_event['source_id'] != ev['source_id']:
            problems.append(f'evidence:{ev["id"]}')
    for d in db.execute('SELECT * FROM decision'):
        if not db.execute('SELECT 1 FROM evidence WHERE run_id=? AND subject=?',
                          (d['run_id'], d['subject'])).fetchone():
            problems.append(f'decision:{d["id"]}')
        if db.execute('SELECT 1 FROM raw_event WHERE subject=?', (d['subject'],)).fetchone() \
                and not db.execute(
                    'SELECT 1 FROM evidence WHERE run_id=? AND subject=? AND event_id IS NOT NULL',
                    (d['run_id'], d['subject'])).fetchone():
            problems.append(f'decision_source:{d["id"]}')
    problems.extend(_verify_cohorts(db))
    db.close()
    return problems
