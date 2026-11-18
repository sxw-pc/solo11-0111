"""Intake, correction planning and versioned restatement for synthetic cohorts.

The intake functions (``record_snapshot`` / ``record_corrections``) only store
source evidence.  ``build_plan`` / ``apply_plan`` turn that evidence into a
frozen, independently checkable chain:

    snapshot raw bytes + members + metric contract
      -> ordered correction notices (original text retained)
      -> authorization run reference
      -> immutable cohort versions (contributor set, mean, dense ranks)

Two historical-effect contracts are frozen up front, never per query:

* ``retroactive_restate``: verified corrections restate the authoritative
  result at the cohort's original effective date; the old product version is
  marked ``superseded`` but its bytes are retained.
* ``prospective_version``: the old version stays authoritative before the
  correction registration instant; the new version only takes effect from it.
"""

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from .store import connect
from .timeline import instant

DERIVED_FIELDS = ('cohort_rank', 'mean')
CONTRACTS = ('retroactive_restate', 'prospective_version')
MEAN_METRIC = 'arithmetic_score'
RANK_METRIC = 'descending_score_dense_ties'
SCORE_FIELD = 'score'

SCHEMA = """
CREATE TABLE IF NOT EXISTS cohort_source (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, digest TEXT NOT NULL UNIQUE,
 raw BLOB NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('snapshot','correction')));
CREATE TABLE IF NOT EXISTS cohort_member (
 cohort_id TEXT NOT NULL, person TEXT NOT NULL, score TEXT NOT NULL,
 source_id INTEGER NOT NULL REFERENCES cohort_source(id),
 PRIMARY KEY(cohort_id,person));
CREATE TABLE IF NOT EXISTS cohort_correction (
 event_key TEXT PRIMARY KEY, cohort_id TEXT NOT NULL, person TEXT NOT NULL,
 field TEXT NOT NULL, old_value TEXT NOT NULL, new_value TEXT NOT NULL,
 at TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES cohort_source(id),
 line_no INTEGER NOT NULL, digest TEXT NOT NULL,
 UNIQUE(source_id,line_no));
CREATE TABLE IF NOT EXISTS cohort_snapshot (
 cohort_id TEXT PRIMARY KEY, dataset_version TEXT, effective_date TEXT,
 metric_contract_json TEXT NOT NULL, source_id INTEGER NOT NULL,
 raw_digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS correction_plan (
 id TEXT PRIMARY KEY, contract TEXT NOT NULL
   CHECK(contract IN ('retroactive_restate','prospective_version')),
 status TEXT NOT NULL CHECK(status IN ('planned','applied')),
 run_id INTEGER, input_digest TEXT NOT NULL, body_json TEXT NOT NULL,
 impact_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 applied_at TEXT);
CREATE TABLE IF NOT EXISTS plan_source (
 plan_id TEXT NOT NULL REFERENCES correction_plan(id),
 source_id INTEGER NOT NULL REFERENCES cohort_source(id),
 PRIMARY KEY(plan_id,source_id));
CREATE TABLE IF NOT EXISTS plan_correction (
 plan_id TEXT NOT NULL REFERENCES correction_plan(id),
 event_key TEXT NOT NULL REFERENCES cohort_correction(event_key),
 PRIMARY KEY(plan_id,event_key));
CREATE TABLE IF NOT EXISTS plan_issue (
 id INTEGER PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES correction_plan(id),
 cohort_id TEXT, person TEXT, event_key TEXT, code TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cohort_version (
 id TEXT NOT NULL UNIQUE, cohort_id TEXT NOT NULL, seq INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('current','superseded','archived')),
 contract TEXT, effective_date TEXT, effective_from TEXT, superseded_at TEXT,
 predecessor_id TEXT REFERENCES cohort_version(id),
 plan_id TEXT REFERENCES correction_plan(id),
 member_set_json TEXT NOT NULL, metric_contract_json TEXT NOT NULL,
 values_json TEXT, corrections_json TEXT NOT NULL,
 source_snapshot_id INTEGER NOT NULL, auth_run_id INTEGER,
 auth_summary_json TEXT NOT NULL, digest TEXT NOT NULL,
 PRIMARY KEY(cohort_id,seq));
CREATE TABLE IF NOT EXISTS cohort_version_correction (
 cohort_id TEXT NOT NULL, version_id TEXT NOT NULL, event_key TEXT NOT NULL,
 PRIMARY KEY(cohort_id,version_id,event_key));
"""


class CohortError(ValueError):
    """Structural cohort problem; code is machine-readable."""

    def __init__(self, code, detail=''):
        super().__init__(code if not detail else f'{code}:{detail}')
        self.code = code
        self.detail = detail


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def digest_of(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def migrate(db):
    db.executescript(SCHEMA)
    _backfill_snapshot_meta(db)
    return db


def _score(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('bad_score') from exc
    if not number.is_finite() or number.as_tuple().exponent < -2:
        raise ValueError('bad_score')
    return str(number)


def _quantize(number):
    return number.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def compute_metrics(scores):
    """Arithmetic mean (2 decimals, half up) and descending dense-tie ranks."""
    decimals = {person: Decimal(value) for person, value in scores.items()}
    mean = _quantize(sum(decimals.values()) / Decimal(len(decimals)))
    distinct = sorted(set(decimals.values()), reverse=True)
    rank_by_score = {score: index + 1 for index, score in enumerate(distinct)}
    ranks = {person: rank_by_score[value] for person, value in decimals.items()}
    return str(mean), ranks


def _backfill_snapshot_meta(db):
    """Populate cohort_snapshot for databases created by older intake code."""
    rows = db.execute('SELECT id,raw FROM cohort_source WHERE kind=\'snapshot\'').fetchall()
    for row in rows:
        value = json.loads(row['raw'])
        cohort_id = value['cohort_id']
        if db.execute('SELECT 1 FROM cohort_snapshot WHERE cohort_id=?', (cohort_id,)).fetchone():
            continue
        contract = value.get('metric_contract') or {}
        db.execute(
            'INSERT INTO cohort_snapshot VALUES (?,?,?,?,?,?)',
            (cohort_id, value.get('dataset_version'), value.get('effective_date'),
             json.dumps(contract, sort_keys=True, ensure_ascii=False),
             row['id'], hashlib.sha256(row['raw']).hexdigest()))


def record_snapshot(path, source_path):
    raw = Path(source_path).read_bytes()
    value = json.loads(raw)
    cohort_id = value['cohort_id']
    rows = value['members']
    if not cohort_id or not isinstance(rows, list) or not rows:
        raise ValueError('bad_cohort_snapshot')
    if len({row['person'] for row in rows}) != len(rows):
        raise ValueError('duplicate_cohort_member')
    db = connect(path)
    try:
        migrate(db)
        digest = hashlib.sha256(raw).hexdigest()
        if db.execute('SELECT 1 FROM cohort_source WHERE digest=?', (digest,)).fetchone():
            return 0
        with db:
            source_id = db.execute(
                "INSERT INTO cohort_source(name,digest,raw,kind) VALUES (?,?,?,'snapshot')",
                (Path(source_path).name, digest, raw)).lastrowid
            for row in rows:
                db.execute('INSERT INTO cohort_member VALUES (?,?,?,?)',
                           (cohort_id, row['person'], _score(row['score']), source_id))
            contract = value.get('metric_contract') or {}
            db.execute(
                'INSERT INTO cohort_snapshot VALUES (?,?,?,?,?,?)',
                (cohort_id, value.get('dataset_version'), value.get('effective_date'),
                 json.dumps(contract, sort_keys=True, ensure_ascii=False),
                 source_id, digest))
        return len(rows)
    finally:
        db.close()


def record_corrections(path, source_path):
    raw = Path(source_path).read_bytes()
    parsed = []
    for line_no, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        for key in ('event_key', 'cohort_id', 'person', 'field', 'old_value', 'new_value', 'at'):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f'correction_missing_{key}')
        if item['field'] != SCORE_FIELD:
            raise ValueError('unsupported_correction_field')
        _score(item['old_value'])
        _score(item['new_value'])
        instant(item['at'])
        parsed.append((line_no, line, item, hashlib.sha256(line).hexdigest()))
    db = connect(path)
    try:
        migrate(db)
        digest = hashlib.sha256(raw).hexdigest()
        if db.execute('SELECT 1 FROM cohort_source WHERE digest=?', (digest,)).fetchone():
            return 0
        with db:
            source_id = db.execute(
                "INSERT INTO cohort_source(name,digest,raw,kind) VALUES (?,?,?,'correction')",
                (Path(source_path).name, digest, raw)).lastrowid
            for line_no, line, item, event_digest in parsed:
                prior = db.execute('SELECT digest FROM cohort_correction WHERE event_key=?',
                                   (item['event_key'],)).fetchone()
                if prior:
                    if prior['digest'] != event_digest:
                        raise ValueError('correction_event_conflict')
                    continue
                db.execute('''INSERT INTO cohort_correction
                    (event_key,cohort_id,person,field,old_value,new_value,at,source_id,line_no,digest)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    (item['event_key'], item['cohort_id'], item['person'], item['field'],
                     item['old_value'], item['new_value'], item['at'], source_id,
                     line_no, event_digest))
        return len(parsed)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Evidence loading and per-cohort verification of the frozen source chain
# --------------------------------------------------------------------------

def _snapshots(db):
    _backfill_snapshot_meta(db)
    return {row['cohort_id']: dict(row)
            for row in db.execute('SELECT * FROM cohort_snapshot')}


def _corrections(db):
    return [dict(r) for r in db.execute(
        'SELECT * FROM cohort_correction ORDER BY at,event_key')]


def _raw_line(db, source_id, line_no):
    source = db.execute('SELECT raw FROM cohort_source WHERE id=?', (source_id,)).fetchone()
    if source is None:
        raise CohortError('source_digest_corrupt', str(source_id))
    lines = source['raw'].splitlines()
    if not 0 < line_no <= len(lines):
        raise CohortError('correction_digest_corrupt', f'line:{line_no}')
    return lines[line_no - 1]


def verify_cohort_evidence(db):
    """Re-hash every source blob and correction line from retained raw bytes.

    Issue tuples are ``(code, cohort_id, person, event_key)``; a corrupt source
    blob fans out to every cohort whose evidence depends on that source.
    """
    issues = []
    snapshots = _snapshots(db)
    for source in db.execute('SELECT * FROM cohort_source'):
        if hashlib.sha256(source['raw']).hexdigest() != source['digest']:
            if source['kind'] == 'snapshot':
                owners = [cid for cid, snap in snapshots.items()
                          if snap['source_id'] == source['id']] or [source['name']]
                for cid in owners:
                    issues.append(('source_digest_corrupt', cid, None, None))
            else:
                owners = {r['cohort_id'] for r in db.execute(
                    'SELECT DISTINCT cohort_id FROM cohort_correction WHERE source_id=?',
                    (source['id'],))} or {source['name']}
                for cid in owners:
                    issues.append(('source_digest_corrupt', cid, None, None))
    for cid, snap in snapshots.items():
        source = db.execute('SELECT raw FROM cohort_source WHERE id=?',
                            (snap['source_id'],)).fetchone()
        try:
            parsed = json.loads(source['raw'])
            members = {r['person']: _score(r['score']) for r in parsed['members']}
        except (ValueError, KeyError, TypeError):
            issues.append(('snapshot_drift', cid, None, None))
            continue
        stored = {r['person']: r['score']
                  for r in db.execute('SELECT person,score FROM cohort_member WHERE cohort_id=?',
                                      (cid,))}
        if members != stored:
            issues.append(('snapshot_drift', cid, None, None))
    for corr in _corrections(db):
        try:
            line = _raw_line(db, corr['source_id'], corr['line_no'])
            if hashlib.sha256(line).hexdigest() != corr['digest']:
                issues.append(('correction_digest_corrupt', corr['cohort_id'],
                               corr['person'], corr['event_key']))
                continue
            if json.loads(line)['event_key'] != corr['event_key']:
                issues.append(('event_text_conflict', corr['cohort_id'],
                               corr['person'], corr['event_key']))
        except CohortError as exc:
            issues.append((exc.code, corr['cohort_id'], corr['person'], corr['event_key']))
    return issues


def _chain_for_cohort(db, cid, members, corrections):
    """Validate ordering/identity of same-field corrections against live values.

    Returns (ordered rows, current scores, issue tuples).  Any issue means the
    cohort must not publish a seemingly complete corrected value.
    """
    issues = []
    current = dict(members)
    cohort_rows = [c for c in corrections if c['cohort_id'] == cid]
    ordered = sorted(cohort_rows, key=lambda c: (instant(c['at']), c['event_key']))
    # Two notices at the same instant for the same person+field are mutually
    # exclusive: no ordering rule can make both authoritative.
    seen_instants = {}
    for corr in ordered:
        slot = (corr['person'], corr['field'], corr['at'])
        if slot in seen_instants:
            issues.append(('mutually_exclusive_corrections', cid, corr['person'],
                           corr['event_key']))
        seen_instants[slot] = corr['event_key']
    for corr in ordered:
        person = corr['person']
        if person not in current:
            issues.append(('correction_member_missing', cid, person, corr['event_key']))
            continue
        if corr['field'] != SCORE_FIELD:
            issues.append(('unsupported_correction_field', cid, person, corr['event_key']))
            continue
        if _score(corr['old_value']) != current[person]:
            issues.append(('old_value_mismatch', cid, person, corr['event_key']))
            continue
        current[person] = _score(corr['new_value'])
    return ordered, current, issues


def build_plan(db_path, contract, run_id=None):
    if contract not in CONTRACTS:
        raise CohortError('unknown_contract', contract)
    db = connect(db_path)
    try:
        migrate(db)
        if run_id is not None and not db.execute('SELECT 1 FROM run WHERE id=?',
                                                  (run_id,)).fetchone():
            raise CohortError('authorization_run_missing', str(run_id))
        evidence_issues = verify_cohort_evidence(db)
        snapshots = _snapshots(db)
        corrections = _corrections(db)
        if not snapshots:
            raise CohortError('no_snapshot')
        member_rows = {}
        for cid in snapshots:
            member_rows[cid] = {r['person']: r['score'] for r in db.execute(
                'SELECT person,score FROM cohort_member WHERE cohort_id=?', (cid,))}
        corrected_persons = {}
        for corr in corrections:
            corrected_persons.setdefault(corr['person'], set()).add(corr['cohort_id'])
        cohorts = {}
        all_issue_rows = []
        for cid, snap in sorted(snapshots.items()):
            metric_contract = json.loads(snap['metric_contract_json'])
            local_issues = [i for i in evidence_issues if i[1] == cid]
            if metric_contract.get('mean') != MEAN_METRIC or \
                    metric_contract.get('rank') != RANK_METRIC:
                local_issues.append(('metric_definition_missing', cid, None, None))
            members = member_rows[cid]
            ordered, current_scores, chain_issues = _chain_for_cohort(
                db, cid, members, corrections)
            local_issues.extend(chain_issues)
            for code, cohort, person, event_key in local_issues:
                all_issue_rows.append((cid, person, event_key, code,
                                       f'{code}/{cohort}/{person or "-"}/{event_key or "-"}'))
            cohorts[cid] = {
                'snapshot_source_id': snap['source_id'],
                'snapshot_digest': snap['raw_digest'],
                'dataset_version': snap['dataset_version'],
                'effective_date': snap['effective_date'],
                'metric_contract': metric_contract,
                'members': sorted(members),
                'member_scores': members,
                'corrections': [c['event_key'] for c in ordered],
                'blocked': bool(local_issues),
                'issue_codes': sorted({i[0] for i in local_issues}),
                'projected_scores': current_scores,
            }
        # Impact closure: a corrected person touches every cohort containing
        # them; each cohort recomputes from its own frozen members.
        impact = {}
        for cid, info in cohorts.items():
            touched = sorted(p for p in corrected_persons
                             if cid in corrected_persons[p] and p in info['members'])
            impact[cid] = {
                'corrected_persons': touched,
                'member_set': info['members'],
                'corrections': info['corrections'],
                'blocked': info['blocked'],
            }
        source_ids = sorted({s['source_id'] for s in snapshots.values()}
                            | {c['source_id'] for c in corrections})
        correction_digests = sorted(c['digest'] for c in corrections)
        snapshot_digests = sorted(s['raw_digest'] for s in snapshots.values())
        input_digest = digest_of({
            'contract': contract, 'run_id': run_id,
            'snapshots': snapshot_digests, 'corrections': correction_digests})
        plan_id = digest_of({
            'contract': contract, 'run_id': run_id,
            'snapshots': snapshot_digests,
            'corrections': sorted((c['event_key'], c['digest']) for c in corrections)})
        body = {'contract': contract, 'run_id': run_id, 'cohorts': cohorts,
                'sources': source_ids}
        existing = db.execute('SELECT status FROM correction_plan WHERE id=?',
                              (plan_id,)).fetchone()
        if existing:
            return {'plan_id': plan_id, 'status': existing['status'], 'idempotent': True}
        with db:
            db.execute('INSERT INTO correction_plan(id,contract,status,run_id,'
                       'input_digest,body_json,impact_json) VALUES (?,?,?,?,?,?,?)',
                       (plan_id, contract, 'planned', run_id, input_digest,
                        json.dumps(body, ensure_ascii=False, sort_keys=True),
                        json.dumps(impact, ensure_ascii=False, sort_keys=True)))
            for source_id in source_ids:
                db.execute('INSERT INTO plan_source VALUES (?,?)', (plan_id, source_id))
            for corr in corrections:
                db.execute('INSERT INTO plan_correction VALUES (?,?)',
                           (plan_id, corr['event_key']))
            for cid, person, event_key, code, detail in all_issue_rows:
                db.execute('INSERT INTO plan_issue(plan_id,cohort_id,person,event_key,'
                           'code,detail) VALUES (?,?,?,?,?,?)',
                           (plan_id, cid, person, event_key, code, detail))
        return {'plan_id': plan_id, 'status': 'planned', 'idempotent': False,
                'impact': impact,
                'blocked_cohorts': sorted(cid for cid, i in impact.items() if i['blocked'])}
    finally:
        db.close()


def _version_digest(payload):
    return digest_of(payload)


def _cohort_subjects(db, cid):
    """Authorization subjects whose derived request names this cohort."""
    row = db.execute('SELECT config_raw FROM batch LIMIT 1').fetchone()
    if row is None:
        return []
    config = json.loads(row[0])
    return sorted(key for key, spec in config.get('subjects', {}).items()
                  if spec.get('cohort') == cid)


def _auth_summary(db, run_id, cid, contributors, source_field):
    config_row = db.execute('SELECT config_hash FROM batch LIMIT 1').fetchone()
    config_hash = config_row['config_hash'] if config_row else None
    subject_keys = _cohort_subjects(db, cid)
    events = []
    for r in db.execute('SELECT * FROM raw_event ORDER BY at,id'):
        payload = json.loads(r['payload'])
        events.append({**dict(r), **payload})
    summary_contributors = []
    for person in contributors:
        keys = sorted(e['event_key'] for e in events
                      if e.get('person') == person
                      and e['subject'] in subject_keys
                      and e['kind'] in ('grant', 'narrow', 'revoke'))
        summary_contributors.append({'person': person, 'source_field': source_field,
                                     'evidence_event_keys': keys})
    return {'run_id': run_id, 'config_hash': config_hash,
            'subjects': subject_keys,
            'contributors': summary_contributors}


def apply_plan(db_path, plan_id=None):
    db = connect(db_path)
    try:
        migrate(db)
        if plan_id is None:
            row = db.execute('SELECT id FROM correction_plan ORDER BY id DESC LIMIT 1').fetchone()
            if row is None:
                raise CohortError('no_plan')
            plan_id = row['id']
        plan = db.execute('SELECT * FROM correction_plan WHERE id=?', (plan_id,)).fetchone()
        if plan is None:
            raise CohortError('unknown_plan', plan_id)
        if plan['status'] == 'applied':
            return {'plan_id': plan_id, 'status': 'applied', 'idempotent': True,
                    'versions': _version_list(db, plan_id)}
        body = json.loads(plan['body_json'])
        contract = body['contract']
        created = []
        # The historical-effect contract is frozen by the first applied plan.
        # Refuse a competing contract explicitly rather than failing on a raw
        # unique-index error; history must not be rewritten per query.
        for cid in sorted(body['cohorts']):
            existing_contract = applied_contract(db, cid)
            if existing_contract is not None and existing_contract != contract:
                raise CohortError(
                    'contract_frozen', f'{cid}:{existing_contract}!={contract}')
        with db:
            for cid, info in sorted(body['cohorts'].items()):
                snap = db.execute('SELECT * FROM cohort_snapshot WHERE cohort_id=?',
                                  (cid,)).fetchone()
                base_scores = dict(info['member_scores'])
                base_members = [
                    {'person': p, 'score': base_scores[p], 'contribution': 'snapshot',
                     'event_keys': []} for p in sorted(base_scores)]
                base_contract = json.loads(snap['metric_contract_json'])
                base_corrections = []
                base_auth = _auth_summary(db, plan['run_id'], cid,
                                          sorted(base_scores), SCORE_FIELD)
                base_mean, base_ranks = compute_metrics(base_scores)
                base_values = {'mean': base_mean,
                               'ranks': base_ranks,
                               'scores': base_scores}
                base_digest = _version_digest({
                    'cohort_id': cid, 'seq': 1, 'members': base_members,
                    'metric_contract': base_contract, 'values': base_values,
                    'corrections': [], 'snapshot_digest': snap['raw_digest']})
                existing_base = db.execute(
                    'SELECT id FROM cohort_version WHERE cohort_id=? AND seq=1', (cid,)).fetchone()
                if existing_base:
                    base_id = existing_base['id']
                else:
                    base_id = f'{cid}@v1'
                    db.execute('''INSERT INTO cohort_version(id,cohort_id,seq,status,contract,
                        effective_date,effective_from,superseded_at,predecessor_id,plan_id,
                        member_set_json,metric_contract_json,values_json,corrections_json,
                        source_snapshot_id,auth_run_id,auth_summary_json,digest)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (base_id, cid, 1, 'current', contract, snap['effective_date'],
                         snap['effective_date'], None, None, plan_id,
                         json.dumps(base_members, sort_keys=True, ensure_ascii=False),
                         json.dumps(base_contract, sort_keys=True, ensure_ascii=False),
                         json.dumps(base_values, sort_keys=True, ensure_ascii=False),
                         json.dumps(base_corrections), snap['source_id'], plan['run_id'],
                         json.dumps(base_auth, sort_keys=True, ensure_ascii=False), base_digest))
                    created.append(base_id)
                if info['blocked'] or not info['corrections']:
                    continue
                ordered = [dict(r) for r in db.execute(
                    'SELECT * FROM cohort_correction WHERE cohort_id=? ORDER BY at,event_key',
                    (cid,))]
                # Consume the scores projected and validated by build_plan; do
                # not silently replay a notice the verification chain rejected.
                corrected_scores = {p: _score(v)
                                    for p, v in info['projected_scores'].items()}
                valid_keys = {c['event_key'] for c in ordered
                              if c['person'] in base_scores}
                member_lookup = {m['person']: m for m in base_members}
                applied_keys = []
                for corr in ordered:
                    if corr['event_key'] not in valid_keys:
                        continue
                    entry = member_lookup[corr['person']]
                    entry['score'] = corrected_scores[corr['person']]
                    entry['contribution'] = 'corrected'
                    entry['event_keys'] = sorted(
                        set(entry['event_keys']) | {corr['event_key']})
                    applied_keys.append(corr['event_key'])
                corrected_members = [member_lookup[p] for p in sorted(member_lookup)]
                corrected_mean, corrected_ranks = compute_metrics(corrected_scores)
                corrected_values = {'mean': corrected_mean,
                                    'ranks': corrected_ranks,
                                    'scores': corrected_scores}
                registration = max(instant(c['at']) for c in ordered).isoformat()
                corrected_auth = _auth_summary(db, plan['run_id'], cid,
                                               sorted(corrected_scores), SCORE_FIELD)
                corrected_digest = _version_digest({
                    'cohort_id': cid, 'seq': 2, 'members': corrected_members,
                    'metric_contract': base_contract, 'values': corrected_values,
                    'corrections': applied_keys, 'snapshot_digest': snap['raw_digest'],
                    'plan_id': plan_id})
                corrected_id = f'{cid}@v2'
                if contract == 'retroactive_restate':
                    db.execute("UPDATE cohort_version SET status='superseded', "
                               "superseded_at=? WHERE id=?", (registration, base_id))
                    v2_status, v2_from = 'current', snap['effective_date']
                else:
                    db.execute("UPDATE cohort_version SET status='archived', "
                               "superseded_at=? WHERE id=?", (registration, base_id))
                    v2_status, v2_from = 'current', registration
                db.execute('''INSERT INTO cohort_version(id,cohort_id,seq,status,contract,
                    effective_date,effective_from,superseded_at,predecessor_id,plan_id,
                    member_set_json,metric_contract_json,values_json,corrections_json,
                    source_snapshot_id,auth_run_id,auth_summary_json,digest)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (corrected_id, cid, 2, v2_status, contract, snap['effective_date'],
                     v2_from, None, base_id, plan_id,
                     json.dumps(corrected_members, sort_keys=True, ensure_ascii=False),
                     json.dumps(base_contract, sort_keys=True, ensure_ascii=False),
                     json.dumps(corrected_values, sort_keys=True, ensure_ascii=False),
                     json.dumps(applied_keys), snap['source_id'], plan['run_id'],
                     json.dumps(corrected_auth, sort_keys=True, ensure_ascii=False),
                     corrected_digest))
                for event_key in applied_keys:
                    db.execute('INSERT INTO cohort_version_correction VALUES (?,?,?)',
                               (cid, corrected_id, event_key))
                created.append(corrected_id)
            db.execute("UPDATE correction_plan SET status='applied', "
                       "applied_at=CURRENT_TIMESTAMP WHERE id=?", (plan_id,))
        return {'plan_id': plan_id, 'status': 'applied', 'idempotent': False,
                'versions': _version_list(db, plan_id), 'created': created}
    finally:
        db.close()


def _version_list(db, plan_id):
    migrate(db)
    rows = db.execute('SELECT id,cohort_id,seq,status,contract FROM cohort_version '
                      'WHERE plan_id=? ORDER BY cohort_id,seq', (plan_id,)).fetchall()
    return [dict(r) for r in rows]


def applied_contract(db, cohort_id):
    row = db.execute('''SELECT p.contract FROM correction_plan p
        JOIN cohort_version v ON v.plan_id=p.id
        WHERE v.cohort_id=? AND p.status='applied' LIMIT 1''', (cohort_id,)).fetchone()
    return row['contract'] if row else None


def resolve_version(db, cohort_id, at_value, contract=None):
    """Choose the authoritative version at ``at_value`` under a frozen contract.

    The contract is fixed when the plan is applied; asking for a different one
    afterwards is refused instead of silently switching history.
    """
    migrate(db)
    versions = [dict(r) for r in db.execute(
        'SELECT * FROM cohort_version WHERE cohort_id=? ORDER BY seq', (cohort_id,))]
    if not versions:
        raise CohortError('cohort_version_missing', cohort_id)
    frozen = applied_contract(db, cohort_id)
    if frozen is None:
        raise CohortError('cohort_not_applied', cohort_id)
    if contract is not None and contract != frozen:
        raise CohortError('contract_mismatch', f'{contract}!={frozen}')
    plan = db.execute('SELECT body_json FROM correction_plan WHERE id=?',
                      (versions[0]['plan_id'],)).fetchone()
    body = json.loads(plan['body_json'])
    if body['cohorts'].get(cohort_id, {}).get('blocked') and len(versions) == 1:
        raise CohortError('cohort_correction_blocked', cohort_id)
    request_time = instant(at_value)
    if frozen == 'retroactive_restate':
        current = next((v for v in versions if v['status'] == 'current'), versions[-1])
        if current['seq'] == 1 and len(versions) > 1:
            raise CohortError('superseded_version_served', cohort_id)
        return current
    # prospective_version
    if len(versions) == 1:
        return versions[0]
    v2 = versions[-1]
    if request_time < instant(v2['effective_from']):
        baseline = versions[0]
        if baseline['status'] != 'archived':
            raise CohortError('version_state_drift', cohort_id)
        return baseline
    if v2['status'] != 'current':
        raise CohortError('version_state_drift', cohort_id)
    return v2


def query_metric(db_path, cohort_id, person, metric, at_value, contract=None):
    db = connect(db_path)
    try:
        migrate(db)
        version = resolve_version(db, cohort_id, at_value, contract)
        values = json.loads(version['values_json'])
        members = json.loads(version['member_set_json'])
        if person not in {m['person'] for m in members}:
            raise CohortError('not_cohort_member', f'{cohort_id}/{person}')
        if metric == 'mean':
            value = values['mean']
        elif metric == 'cohort_rank':
            value = values['ranks'][person]
        else:
            raise CohortError('unknown_metric', metric)
        return {'cohort_id': cohort_id, 'person': person, 'metric': metric,
                'at': at_value, 'value': value, 'version_id': version['id'],
                'version_status': version['status'],
                'contract': version['contract'], 'digest': version['digest']}
    finally:
        db.close()


def list_plans(db_path):
    db = connect(db_path)
    try:
        migrate(db)
        return [dict(r) for r in db.execute(
            'SELECT id,contract,status,run_id,applied_at FROM correction_plan ORDER BY id')]
    finally:
        db.close()


def plan_issues(db_path, plan_id):
    db = connect(db_path)
    try:
        migrate(db)
        return [dict(r) for r in db.execute(
            'SELECT * FROM plan_issue WHERE plan_id=? ORDER BY id', (plan_id,))]
    finally:
        db.close()
