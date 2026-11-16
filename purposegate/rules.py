import json
from .store import connect
from .timeline import active_grant,instant
def _config(db):return json.loads(db.execute('SELECT config_raw FROM batch LIMIT 1').fetchone()[0])
def _allowed_purpose(requested,granted,tree):
    seen=set();current=requested
    while current and current not in seen:
        if current==granted:return True
        seen.add(current);current=tree.get(current)
    if current in seen:raise ValueError('purpose_cycle')
    return False
def _ancestors(field,lineage):
    seen=set();visiting=set()
    def visit(item):
        if item in visiting:raise ValueError('lineage_cycle')
        if item in seen:return
        visiting.add(item)
        for parent in lineage.get(item,[]):visit(parent)
        visiting.remove(item);seen.add(item)
    visit(field);return seen
def evaluate(path):
    db=connect(path);cfg=_config(db);events=[json.loads(r['payload']) for r in db.execute('SELECT * FROM raw_event ORDER BY at,id')]
    result={}
    with db:
        rid=db.execute("INSERT INTO run(batch_id,status) VALUES (?, '已校验')",(cfg['batch_id'],)).lastrowid
        for key,spec in cfg['subjects'].items():
            issues=[];reasons={};per_person={};persons=spec['persons'];requested=spec['fields'];valid_fields=set(cfg['dataset']['fields'])
            if not persons:issues.append('empty_subject_set')
            if spec['version']!=cfg['dataset']['version']:
                mapping=cfg.get('version_mappings',{}).get(spec['version'])
                if not mapping:issues.append('version_mismatch')
                else:
                    names=list(dict.fromkeys([*requested,*spec['required']]))
                    if any(f not in mapping for f in names) or len({mapping[f] for f in names if f in mapping})!=len(names):
                        issues.append('ambiguous_version_mapping')
                    else:
                        requested=[mapping[f] for f in requested]
                        spec={**spec,'required':[mapping[f] for f in spec['required']]}
            if any(f not in valid_fields for f in requested):issues.append('unknown_field')
            if spec['purpose'] not in cfg['purpose_tree'] and spec['purpose'] not in cfg['purpose_roots']:issues.append('unknown_purpose')
            for person in persons:
                history=[e for e in events if e['subject']==key and e['kind'] in ('grant','narrow','revoke') and e.get('person')==person]
                grant=active_grant(history,spec['at'])
                try:purpose_ok=grant is not None and _allowed_purpose(spec['purpose'],grant['purpose'],cfg['purpose_tree'])
                except ValueError:
                    issues.append('purpose_cycle');reasons[person]='purpose_cycle';per_person[person]=set();continue
                if not grant or not purpose_ok or not instant(grant['from'])<=instant(spec['at'])<instant(grant['until']):
                    issues.append('no_valid_grant');reasons[person]='no_valid_grant';per_person[person]=set();continue
                person_fields=set()
                for field in requested:
                    try:
                        ancestors=_ancestors(field,cfg['lineage'])
                        if not ancestors<=valid_fields:issues.append('unknown_field');continue
                        if ancestors<=set(grant['fields']):person_fields.add(field)
                    except ValueError:issues.append('lineage_cycle')
                if not set(spec['required'])<=person_fields:issues.append('required_denied')
                per_person[person]=person_fields;reasons[person]=sorted(person_fields)
            # A delivery covering several people is limited to fields licensed for
            # every person; a union here would disclose a field on behalf of someone
            # who never granted it. Keep per-person partitions for review.
            allowed=set.intersection(*(per_person.get(p,set()) for p in persons)) if persons else set()
            if requested and not allowed and len(persons)>1:issues.append('no_common_field')
            if issues and any(code in issues for code in ('version_mismatch','ambiguous_version_mapping','unknown_field','lineage_cycle','unknown_purpose','purpose_cycle')):status='无法判定'
            elif issues:status='拒绝'
            else:status='允许'
            detail=json.dumps({'allowed_fields':sorted(allowed),'persons':reasons,
                               'partitions':{p:{'allowed_fields':sorted(per_person.get(p,set())),
                                                'status':'允许' if p in per_person and set(spec['required'])<=per_person[p] else '拒绝'}
                                             for p in persons},'issues':sorted(set(issues))},ensure_ascii=False)
            for code in sorted(set(issues)):db.execute('INSERT INTO issue(run_id,subject,code,detail) VALUES (?,?,?,?)',(rid,key,code,code))
            db.execute('INSERT INTO evidence(run_id,subject,rule_id,detail) VALUES (?,?,?,?)',(rid,key,cfg['rule_version'],detail))
            db.execute('INSERT INTO decision(run_id,subject,status,detail) VALUES (?,?,?,?)',(rid,key,status,detail));result[key]=status
        db.execute("UPDATE run SET status=CASE WHEN EXISTS(SELECT 1 FROM decision WHERE run_id=? AND status='无法判定') THEN '待核' ELSE '已判定' END,ended_at=CURRENT_TIMESTAMP WHERE id=?",(rid,rid))
    db.close();return result
def action(command,path,sandbox=None,out=None):
    from .report import export,verify
    if command=='plan':return export(path,out or 'permission-report.json')
    if command=='recheck':return evaluate(path)
    raise ValueError('unsupported_action')
def verify_decisions(path):
    import tempfile,sqlite3
    from pathlib import Path
    db=connect(path);old={r['subject']:r['status'] for r in db.execute('SELECT subject,status FROM decision WHERE run_id=(SELECT max(id) FROM run)')}
    with tempfile.TemporaryDirectory() as tmp:
        copy=Path(tmp)/'check.db';other=sqlite3.connect(copy);db.backup(other);other.close()
        computed=evaluate(copy)
    db.close();return [f'decision_mismatch:{k}' for k in old.keys()|computed.keys() if old.get(k)!=computed.get(k)]
