import json
from .store import connect
from .timeline import (AMBIGUOUS_CODES, DELEGATION_KINDS, active_grant, allowed_purpose,
                       field_ancestors, instant, resolve_delegation)

_allowed_purpose = allowed_purpose
_ancestors = field_ancestors


def _config(db):
    return json.loads(db.execute('SELECT config_raw FROM batch LIMIT 1').fetchone()[0])


def _persist_delegation(db, rid, key, person, result, index):
    """Freeze one person's chain evidence for this run only."""
    for chain in result.get('chains', []):
        contract = chain.get('contract') or 'unknown'
        source = chain.get('contract_source') or {}
        source_row = index.get(source.get('event_key')) or {}
        contract_sha = source_row.get('digest') or source.get('source_sha')
        fields = json.dumps(chain.get('field_intersection', []), ensure_ascii=False)
        detail = json.dumps({
            'issues': chain.get('issues', []),
            'allowed_fields': chain.get('allowed_fields', []),
            'field_intersection': chain.get('field_intersection', []),
            'effective_until': chain.get('effective_until'),
            'revoked_at': chain.get('revoked_at'),
            'revocation_effect': chain.get('revocation_effect'),
            # The table column carries the raw source-line digest; keep the
            # canonical event hash here so both are available for review.
            'contract_source': {**source, 'source_sha': contract_sha},
        }, ensure_ascii=False, sort_keys=True)
        chain_id = db.execute(
            'INSERT INTO delegation_chain(run_id,subject,final_user,chain_key,status,contract,'
            'contract_event_key,contract_source_sha,contract_recorded_at,field_intersection,detail) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (rid, key, result.get('final_user') or person, chain.get('chain_key'), chain.get('status'),
             contract, source.get('event_key'), contract_sha, source.get('signed_at'),
             fields, detail)).lastrowid
        for link in chain.get('links', []):
            row = index.get(link.get('event_key')) or {}
            link_detail = json.dumps({k: link.get(k) for k in (
                'status', 'detail', 'field_intersection', 'effective_until', 'issues')},
                ensure_ascii=False, sort_keys=True)
            db.execute(
                'INSERT INTO delegation_link(chain_id,level,event_key,event_id,source_id,source_sha,'
                'delegator,delegatee,purpose,fields_json,field_intersection,from_at,until_at,signed_at,'
                'revocation_effect,irrevocable_until,status,effective_until,detail) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (chain_id, link.get('level'), link.get('event_key'), row.get('id'), row.get('source_id'),
                 row.get('digest') or link.get('source_sha'), link.get('delegator'), link.get('delegatee'),
                 link.get('purpose'), json.dumps(link.get('fields', []), ensure_ascii=False),
                 json.dumps(link.get('field_intersection', []), ensure_ascii=False),
                 link.get('from_at'), link.get('until_at'), link.get('signed_at'),
                 link.get('revocation_effect'), link.get('irrevocable_until'), link.get('status'),
                 link.get('effective_until'), link_detail))


def _event_rows(db):
    return [dict(r) for r in db.execute('SELECT * FROM raw_event ORDER BY at,id')]


def evaluate(path):
    db = connect(path); cfg = _config(db)
    event_rows = _event_rows(db)
    events = [json.loads(r['payload']) for r in event_rows]
    event_by_key = {json.loads(r['payload'])['event_key']: r for r in event_rows}
    event_index = event_by_key
    result = {}
    with db:
        rid = db.execute("INSERT INTO run(batch_id,status) VALUES (?, '已校验')", (cfg['batch_id'],)).lastrowid
        for key, spec in cfg['subjects'].items():
            issues = []; reasons = {}; per_person = {}; used_keys = set(); persons = spec['persons']
            requested = spec['fields']; valid_fields = set(cfg['dataset']['fields'])
            person_status = {}
            if not persons: issues.append('empty_subject_set')
            if spec['version'] != cfg['dataset']['version']:
                mapping = cfg.get('version_mappings', {}).get(spec['version'])
                if not mapping: issues.append('version_mismatch')
                else:
                    names = list(dict.fromkeys([*requested, *spec['required']]))
                    if any(f not in mapping for f in names) or len({mapping[f] for f in names if f in mapping}) != len(names):
                        issues.append('ambiguous_version_mapping')
                    else:
                        requested = [mapping[f] for f in requested]
                        spec = {**spec, 'required': [mapping[f] for f in spec['required']]}
            if any(f not in valid_fields for f in requested): issues.append('unknown_field')
            if spec['purpose'] not in cfg['purpose_tree'] and spec['purpose'] not in cfg['purpose_roots']: issues.append('unknown_purpose')
            delegatees = spec.get('delegatees') or {}
            conflict_keys = set()
            for code, detail in db.execute('SELECT code,detail FROM issue WHERE subject=? AND code=?',
                                           (key, 'identity_conflict')):
                conflict_keys.add(str(detail))
            for person in persons:
                if person in delegatees:
                    final_user = delegatees[person]
                    scoped = [e for e in events if e.get('subject') == key and e.get('kind') in DELEGATION_KINDS]
                    outcome = resolve_delegation(scoped, spec['at'], person, final_user,
                                                 spec['purpose'], requested, cfg['purpose_tree'],
                                                 cfg['lineage'], valid_fields,
                                                 extra_conflicts=conflict_keys)
                    outcome['final_user'] = final_user
                    used_keys.update(outcome.get('used_keys', set()))
                    _persist_delegation(db, rid, key, person, outcome, event_index)
                    person_fields = set(outcome['fields'])
                    if outcome['status'] == '允许':
                        if not set(spec['required']) <= person_fields:
                            issues.append('required_denied')
                            person_status[person] = '拒绝'
                        else:
                            person_status[person] = '允许'
                        reasons[person] = sorted(person_fields)
                    else:
                        issues.extend(outcome['issues'])
                        reasons[person] = []
                        person_status[person] = outcome['status']
                    per_person[person] = person_fields
                    continue
                history = [e for e in events if e['subject'] == key and e['kind'] in ('grant', 'narrow', 'revoke') and e.get('person') == person]
                used_keys.update(e['event_key'] for e in history if instant(e['at']) <= instant(spec['at']))
                grant = active_grant(history, spec['at'])
                try:
                    purpose_ok = grant is not None and _allowed_purpose(spec['purpose'], grant['purpose'], cfg['purpose_tree'])
                except ValueError:
                    issues.append('purpose_cycle'); reasons[person] = 'purpose_cycle'
                    per_person[person] = set(); person_status[person] = '无法判定'; continue
                if not grant or not purpose_ok or not instant(grant['from']) <= instant(spec['at']) < instant(grant['until']):
                    issues.append('no_valid_grant'); reasons[person] = 'no_valid_grant'
                    per_person[person] = set(); person_status[person] = '拒绝'; continue
                person_fields = set()
                for field in requested:
                    try:
                        ancestors = _ancestors(field, cfg['lineage'])
                        if not ancestors <= valid_fields: issues.append('unknown_field'); continue
                        if ancestors <= set(grant['fields']): person_fields.add(field)
                    except ValueError: issues.append('lineage_cycle')
                if not set(spec['required']) <= person_fields: issues.append('required_denied')
                per_person[person] = person_fields; reasons[person] = sorted(person_fields)
                person_status[person] = '允许' if set(spec['required']) <= person_fields else '拒绝'
            # A delivery covering several people is limited to fields licensed for
            # every person; a union here would disclose a field on behalf of someone
            # who never granted it. Keep per-person partitions for review.
            allowed = set.intersection(*(per_person.get(p, set()) for p in persons)) if persons else set()
            if requested and not allowed and len(persons) > 1: issues.append('no_common_field')
            if issues and any(code in issues for code in ('version_mismatch', 'ambiguous_version_mapping', 'unknown_field', 'lineage_cycle', 'unknown_purpose', 'purpose_cycle')) or any(code in AMBIGUOUS_CODES for code in issues):
                status = '无法判定'
            elif issues: status = '拒绝'
            else: status = '允许'
            detail = json.dumps({'allowed_fields': sorted(allowed), 'persons': reasons,
                                 'partitions': {p: {'allowed_fields': sorted(per_person.get(p, set())),
                                                    'status': person_status.get(p, '允许' if p in per_person and set(spec['required']) <= per_person[p] else '拒绝')}
                                                for p in persons}, 'issues': sorted(set(issues))}, ensure_ascii=False)
            for code in sorted(set(issues)): db.execute('INSERT INTO issue(run_id,subject,code,detail) VALUES (?,?,?,?)', (rid, key, code, code))
            db.execute('INSERT INTO evidence(run_id,subject,rule_id,detail) VALUES (?,?,?,?)', (rid, key, cfg['rule_version'], detail))
            for event_key in sorted(used_keys):
                event = event_by_key.get(event_key)
                if event is None: continue
                db.execute('INSERT INTO evidence(run_id,subject,source_id,event_id,rule_id,detail) VALUES (?,?,?,?,?,?)',
                           (rid, key, event['source_id'], event['id'], cfg['rule_version'], 'authorization_history'))
            db.execute('INSERT INTO decision(run_id,subject,status,detail) VALUES (?,?,?,?)', (rid, key, status, detail)); result[key] = status
        db.execute("UPDATE run SET status=CASE WHEN EXISTS(SELECT 1 FROM decision WHERE run_id=? AND status='无法判定') THEN '待核' ELSE '已判定' END,ended_at=CURRENT_TIMESTAMP WHERE id=?", (rid, rid))
    db.close(); return result


def action(command, path, sandbox=None, out=None):
    from .report import export, verify
    if command == 'plan': return export(path, out or 'permission-report.json')
    if command == 'recheck': return evaluate(path)
    raise ValueError('unsupported_action')


def verify_decisions(path):
    import tempfile, sqlite3
    from pathlib import Path
    db = connect(path)
    old = {r['subject']: r['status'] for r in db.execute('SELECT subject,status FROM decision WHERE run_id=(SELECT max(id) FROM run)')}
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / 'check.db'; other = sqlite3.connect(copy); db.backup(other); other.close()
        computed = evaluate(copy)
    db.close()
    return [f'decision_mismatch:{k}' for k in old.keys() | computed.keys() if old.get(k) != computed.get(k)]
