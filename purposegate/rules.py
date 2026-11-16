import json
from .store import connect
def _config(db):return json.loads(db.execute('SELECT config_raw FROM batch LIMIT 1').fetchone()[0])
def _allowed_purpose(requested,granted,tree):
    seen=set();current=requested
    while current and current not in seen:
        if current==granted:return True
        seen.add(current);current=tree.get(current)
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
            issues=[];reasons={};allowed=set();persons=spec['persons'];requested=spec['fields'];valid_fields=set(cfg['dataset']['fields'])
            if spec['version']!=cfg['dataset']['version']:
                mapping=cfg.get('version_mappings',{}).get(spec['version'])
                if not mapping:issues.append('version_mismatch')
                else:
                    requested=[mapping.get(f,f) for f in requested]
                    spec={**spec,'required':[mapping.get(f,f) for f in spec['required']]}
            if any(f not in valid_fields for f in requested):issues.append('unknown_field')
            if spec['purpose'] not in cfg['purpose_tree'] and spec['purpose'] not in cfg['purpose_roots']:issues.append('unknown_purpose')
            for person in persons:
                history=[e for e in events if e['subject']==key and e['kind'] in ('grant','narrow','revoke') and e.get('person')==person and e['at']<=spec['at']]
                history.sort(key=lambda x:(x['at'],x['event_key']))
                grant=None
                for e in history:
                    if e['kind']=='revoke':grant=None
                    elif e['kind']=='grant':grant=e
                    elif e['kind']=='narrow' and grant:grant={**grant,'fields':list(set(grant['fields']) & set(e['fields']))}
                if not grant or not _allowed_purpose(spec['purpose'],grant['purpose'],cfg['purpose_tree']) or not grant['from']<=spec['at']<grant['until']:
                    issues.append('no_valid_grant');reasons[person]='no_valid_grant';continue
                person_fields=set()
                for field in requested:
                    try:
                        ancestors=_ancestors(field,cfg['lineage'])
                        if not ancestors<=valid_fields:issues.append('unknown_field');continue
                        if ancestors<=set(grant['fields']):person_fields.add(field)
                    except ValueError:issues.append('lineage_cycle')
                if not set(spec['required'])<=person_fields:issues.append('required_denied')
                allowed.update(person_fields);reasons[person]=sorted(person_fields)
            if issues and any(code in issues for code in ('version_mismatch','unknown_field','lineage_cycle','unknown_purpose')):status='无法判定'
            elif issues:status='拒绝'
            else:status='允许'
            detail=json.dumps({'allowed_fields':sorted(allowed),'persons':reasons,'issues':sorted(set(issues))},ensure_ascii=False)
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
