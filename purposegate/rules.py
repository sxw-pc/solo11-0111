import json
from .store import connect
from .timeline import active_grant, instant
from .cohort import (
    DERIVED_FIELDS, SCORE_FIELD, CohortError, migrate as cohort_migrate,
    resolve_version,
)

# Structural/evidence problems on derived cohort outputs are never downgraded
# to a plain denial: without contributor evidence the value is undecidable.
COHORT_UNDECIDABLE_CODES = frozenset({
    'cohort_version_missing', 'cohort_not_applied', 'contract_mismatch',
    'cohort_correction_blocked', 'cohort_reference_missing',
    'superseded_version_served', 'version_state_drift',
    'contributor_evidence_gap', 'unknown_derived_field', 'unknown_metric',
})


def _config(db):
    return json.loads(db.execute('SELECT config_raw FROM batch LIMIT 1').fetchone()[0])


def _allowed_purpose(requested, granted, tree):
    seen = set()
    current = requested
    while current and current not in seen:
        if current == granted:
            return True
        seen.add(current)
        current = tree.get(current)
    if current in seen:
        raise ValueError('purpose_cycle')
    return False


def _ancestors(field, lineage):
    seen = set()
    visiting = set()

    def visit(item):
        if item in visiting:
            raise ValueError('lineage_cycle')
        if item in seen:
            return
        visiting.add(item)
        for parent in lineage.get(item, []):
            visit(parent)
        visiting.remove(item)
        seen.add(item)

    visit(field)
    return seen


def _cohort_gate(db, spec, cfg, events, event_by_key, key, rid, used_keys):
    """Authorize a derived cohort value against every bound contributor.

    The frozen cohort version names the real contributor set, not only field
    names.  Each contributor must individually be licensed for the request
    purpose and the source ``score`` field at the request instant; one
    requester's own licence can never cover another contributor.
    """
    issues = []
    derived = [f for f in spec['fields'] if f in DERIVED_FIELDS]
    if not derived:
        return issues, set(), None
    cohort_id = spec.get('cohort')
    if not cohort_id:
        issues.append('cohort_reference_missing')
        return issues, set(), None
    try:
        version = resolve_version(db, cohort_id, spec['at'])
    except CohortError as exc:
        issues.append(exc.code)
        return issues, set(), None
    contributors = json.loads(version['member_set_json'])
    try:
        score_ancestors = _ancestors(SCORE_FIELD, cfg['lineage'])
        if not score_ancestors <= set(cfg['dataset']['fields']):
            issues.append('unknown_field')
            return issues, set(), None
    except ValueError:
        issues.append('lineage_cycle')
        return issues, set(), None
    partitions = {}
    gate_ok = True
    for member in contributors:
        person = member['person']
        history = [e for e in events
                   if e['subject'] == key and e['kind'] in ('grant', 'narrow', 'revoke')
                   and e.get('person') == person]
        used_keys.update(e['event_key'] for e in history
                         if instant(e['at']) <= instant(spec['at']))
        grant = active_grant(history, spec['at'])
        try:
            purpose_ok = grant is not None and _allowed_purpose(
                spec['purpose'], grant['purpose'], cfg['purpose_tree'])
        except ValueError:
            issues.append('purpose_cycle')
            partitions[person] = {'status': '无法判定', 'reason': 'purpose_cycle'}
            gate_ok = False
            continue
        field_ok = grant is not None and score_ancestors <= set(grant['fields'])
        time_ok = grant is not None and instant(grant['from']) <= instant(spec['at']) \
            < instant(grant['until'])
        person_ok = bool(grant and purpose_ok and field_ok and time_ok)
        if not person_ok:
            gate_ok = False
            reason = 'no_valid_grant' if not (grant and time_ok and purpose_ok) \
                else 'source_field_not_licensed'
            partitions[person] = {'status': '拒绝', 'reason': reason,
                                  'evidence_event_keys': sorted(
                                      e['event_key'] for e in history)}
        else:
            partitions[person] = {'status': '允许', 'reason': 'contributor_licensed',
                                  'evidence_event_keys': sorted(
                                      e['event_key'] for e in history)}
    if not gate_ok:
        # Stale score licences cannot keep a derived output alive: the missing
        # contributor evidence blocks the whole versioned value.
        issues.append('contributor_evidence_gap')
    detail = {
        'cohort_id': cohort_id, 'version_id': version['id'],
        'version_digest': version['digest'], 'contract': version['contract'],
        'source_field': SCORE_FIELD, 'derived_fields': derived,
        'contributors': [m['person'] for m in contributors],
        'contributor_partitions': partitions,
        'status': '允许' if gate_ok else '无法判定',
    }
    db.execute('INSERT INTO evidence(run_id,subject,rule_id,detail) VALUES (?,?,?,?)',
               (rid, key, cfg['rule_version'],
                json.dumps({'cohort_derived': detail}, ensure_ascii=False)))
    return issues, (set(derived) if gate_ok else set()), detail


def evaluate(path):
    db = connect(path)
    cohort_migrate(db)
    cfg = _config(db)
    event_rows = [dict(r) for r in db.execute('SELECT * FROM raw_event ORDER BY at,id')]
    events = [json.loads(r['payload']) for r in event_rows]
    event_by_key = {json.loads(r['payload'])['event_key']: r for r in event_rows}
    result = {}
    with db:
        rid = db.execute("INSERT INTO run(batch_id,status) VALUES (?, '已校验')",
                         (cfg['batch_id'],)).lastrowid
        for key, spec in cfg['subjects'].items():
            issues = []
            reasons = {}
            per_person = {}
            used_keys = set()
            persons = spec['persons']
            requested_all = list(spec['fields'])
            derived_requested = [f for f in requested_all if f in DERIVED_FIELDS]
            requested = [f for f in requested_all if f not in DERIVED_FIELDS]
            valid_fields = set(cfg['dataset']['fields'])
            if not persons:
                issues.append('empty_subject_set')
            if spec['version'] != cfg['dataset']['version']:
                mapping = cfg.get('version_mappings', {}).get(spec['version'])
                if not mapping:
                    issues.append('version_mismatch')
                else:
                    names = list(dict.fromkeys([*requested, *spec['required']]))
                    if any(f not in mapping for f in names) or len(
                            {mapping[f] for f in names if f in mapping}) != len(names):
                        issues.append('ambiguous_version_mapping')
                    else:
                        requested = [mapping[f] for f in requested]
                        spec = {**spec, 'required': [mapping[f] for f in spec['required']]}
            if any(f not in valid_fields for f in requested):
                issues.append('unknown_field')
            if spec['purpose'] not in cfg['purpose_tree'] and \
                    spec['purpose'] not in cfg['purpose_roots']:
                issues.append('unknown_purpose')
            for person in persons:
                history = [e for e in events if e['subject'] == key
                           and e['kind'] in ('grant', 'narrow', 'revoke')
                           and e.get('person') == person]
                used_keys.update(e['event_key'] for e in history
                                 if instant(e['at']) <= instant(spec['at']))
                grant = active_grant(history, spec['at'])
                try:
                    purpose_ok = grant is not None and _allowed_purpose(
                        spec['purpose'], grant['purpose'], cfg['purpose_tree'])
                except ValueError:
                    issues.append('purpose_cycle')
                    reasons[person] = 'purpose_cycle'
                    per_person[person] = set()
                    continue
                if not grant or not purpose_ok or not instant(grant['from']) <= \
                        instant(spec['at']) < instant(grant['until']):
                    issues.append('no_valid_grant')
                    reasons[person] = 'no_valid_grant'
                    per_person[person] = set()
                    continue
                person_fields = set()
                for field in requested:
                    try:
                        ancestors = _ancestors(field, cfg['lineage'])
                        if not ancestors <= valid_fields:
                            issues.append('unknown_field')
                            continue
                        if ancestors <= set(grant['fields']):
                            person_fields.add(field)
                    except ValueError:
                        issues.append('lineage_cycle')
                if not set(spec['required']) <= person_fields:
                    issues.append('required_denied')
                per_person[person] = person_fields
                reasons[person] = sorted(person_fields)
            # A delivery covering several people is limited to fields licensed
            # for every person; a union here would disclose a field on behalf
            # of someone who never granted it. Keep per-person partitions.
            allowed = set.intersection(
                *(per_person.get(p, set()) for p in persons)) if persons else set()
            if requested and not allowed and len(persons) > 1:
                issues.append('no_common_field')
            cohort_detail = None
            if derived_requested:
                cohort_issues, derived_allowed, cohort_detail = _cohort_gate(
                    db, spec, cfg, events, event_by_key, key, rid, used_keys)
                issues.extend(cohort_issues)
                allowed |= derived_allowed
            if issues and any(code in issues for code in (
                    'version_mismatch', 'ambiguous_version_mapping', 'unknown_field',
                    'lineage_cycle', 'unknown_purpose', 'purpose_cycle',
                    *COHORT_UNDECIDABLE_CODES)):
                status = '无法判定'
            elif issues:
                status = '拒绝'
            else:
                status = '允许'
            detail_payload = {
                'allowed_fields': sorted(allowed), 'persons': reasons,
                'partitions': {p: {
                    'allowed_fields': sorted(per_person.get(p, set())),
                    'status': '允许' if p in per_person
                    and set(spec['required']) <= per_person[p] else '拒绝'}
                    for p in persons},
                'issues': sorted(set(issues))}
            if cohort_detail is not None:
                detail_payload['cohort_derived'] = cohort_detail
            detail = json.dumps(detail_payload, ensure_ascii=False)
            for code in sorted(set(issues)):
                db.execute('INSERT INTO issue(run_id,subject,code,detail) VALUES (?,?,?,?)',
                           (rid, key, code, code))
            db.execute('INSERT INTO evidence(run_id,subject,rule_id,detail) VALUES (?,?,?,?)',
                       (rid, key, cfg['rule_version'], detail))
            for event_key in sorted(used_keys):
                event = event_by_key[event_key]
                db.execute('INSERT INTO evidence(run_id,subject,source_id,event_id,rule_id,'
                           'detail) VALUES (?,?,?,?,?,?)',
                           (rid, key, event['source_id'], event['id'], cfg['rule_version'],
                            'authorization_history'))
            db.execute('INSERT INTO decision(run_id,subject,status,detail) VALUES (?,?,?,?)',
                       (rid, key, status, detail))
            result[key] = status
        db.execute("UPDATE run SET status=CASE WHEN EXISTS(SELECT 1 FROM decision WHERE run_id=? "
                   "AND status='无法判定') THEN '待核' ELSE '已判定' END,ended_at=CURRENT_TIMESTAMP "
                   "WHERE id=?", (rid, rid))
    db.close()
    return result


def action(command, path, sandbox=None, out=None):
    if command == 'plan':
        from .report import export
        return export(path, out or 'permission-report.json')
    if command == 'recheck':
        return evaluate(path)
    raise ValueError('unsupported_action')


def verify_decisions(path):
    import tempfile
    import sqlite3
    from pathlib import Path
    db = connect(path)
    if not db.execute('SELECT 1 FROM batch LIMIT 1').fetchone():
        # Cohort-only ledger: no authorization decisions exist to recheck.
        db.close()
        return []
    old = {r['subject']: r['status'] for r in db.execute(
        'SELECT subject,status FROM decision WHERE run_id=(SELECT max(id) FROM run)')}
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / 'check.db'
        other = sqlite3.connect(copy)
        db.backup(other)
        other.close()
        computed = evaluate(copy)
    db.close()
    return [f'decision_mismatch:{k}'
            for k in old.keys() | computed.keys() if old.get(k) != computed.get(k)]
