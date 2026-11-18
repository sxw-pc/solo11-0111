"""Post-delivery use audit over a frozen package and evolving grants."""
import json
from pathlib import Path

from .delivery import SCHEMA as DELIVERY_SCHEMA, _digest
from .rules import _allowed_purpose, _ancestors
from .store import connect
from .timeline import active_grant, instant

SCHEMA="""
CREATE TABLE IF NOT EXISTS usage_event(
 event_key TEXT PRIMARY KEY, package_id TEXT NOT NULL REFERENCES delivery_package(id),
 payload TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage_audit(
 event_key TEXT NOT NULL REFERENCES usage_event(event_key), audit_run INTEGER NOT NULL,
 status TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(event_key,audit_run));
CREATE TABLE IF NOT EXISTS usage_run(
 id INTEGER PRIMARY KEY, package_id TEXT NOT NULL REFERENCES delivery_package(id),
 status TEXT NOT NULL, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""

def record(db_path, records):
    db=connect(db_path);db.executescript(DELIVERY_SCHEMA+SCHEMA)
    with db:
        for row in records:
            for key in ('event_key','package_id','person','field','purpose','at'):
                if key not in row:raise ValueError(f'usage_missing:{key}')
            instant(row['at'])
            digest=_digest(row)
            old=db.execute('SELECT digest FROM usage_event WHERE event_key=?',(row['event_key'],)).fetchone()
            if old and old['digest']!=digest:raise ValueError('usage_identity_conflict')
            if not old:db.execute('INSERT INTO usage_event VALUES (?,?,?,?)',
                                  (row['event_key'],row['package_id'],json.dumps(row,ensure_ascii=False),digest))
    db.close();return len(records)

def audit(db_path, package_id):
    db=connect(db_path);db.executescript(DELIVERY_SCHEMA+SCHEMA)
    package=db.execute('SELECT * FROM delivery_package WHERE id=?',(package_id,)).fetchone()
    if not package:raise ValueError('unknown_delivery')
    config=json.loads(db.execute('SELECT config_raw FROM batch LIMIT 1').fetchone()[0])
    spec=config['subjects'][package['request_id']]
    authorized=set(json.loads(package['fields_json']))
    source=[json.loads(r['payload']) for r in db.execute('SELECT payload FROM raw_event ORDER BY at,id')]
    result=[]
    output=Path(package['output_path']) if package['output_path'] else None
    package_ok=False
    if output and output.exists():
        try:
            payload=json.loads(output.read_text(encoding='utf-8'))
            package_ok=(payload['manifest_hash']==package['manifest_hash']
                        and _digest(payload['fields'])==package['values_hash'])
        except (KeyError,ValueError,UnicodeError): pass
    with db:
        audit_run=db.execute('INSERT INTO usage_run(package_id,status) VALUES (?,?)',(package_id,'running')).lastrowid
        for r in db.execute('SELECT * FROM usage_event WHERE package_id=? ORDER BY event_key',(package_id,)):
            event=json.loads(r['payload']);person=event['person'];field=event['field'];purpose=event['purpose'];when=event['at']
            status='compliant';reason='authorized_at_use'
            if package['state']!='released' or not package_ok:
                status='uncertain';reason='delivery_evidence_missing'
            elif person not in spec['persons'] or field not in authorized:
                status='violation';reason='person_or_field_outside_package'
            else:
                history=[e for e in source if e['subject']==package['request_id'] and e.get('person')==person
                         and e['kind'] in ('grant','narrow','revoke')]
                grant=active_grant(history,when)
                try:
                    ancestors=_ancestors(field,config['lineage'])
                    allowed=(grant is not None and _allowed_purpose(purpose,grant['purpose'],config['purpose_tree'])
                             and instant(grant['from'])<=instant(when)<instant(grant['until'])
                             and ancestors<=set(grant['fields']))
                except ValueError:
                    allowed=False;status='uncertain';reason='contract_ambiguous'
                if not allowed and status=='compliant':status='violation';reason='grant_no_longer_covers_use'
            db.execute('INSERT INTO usage_audit VALUES (?,?,?,?)',(r['event_key'],audit_run,status,reason))
            result.append({'event_key':r['event_key'],'status':status,'reason':reason})
        overall='violation' if any(x['status']=='violation' for x in result) else (
            'uncertain' if any(x['status']=='uncertain' for x in result) or not result else 'compliant')
        db.execute('UPDATE usage_run SET status=? WHERE id=?',(overall,audit_run))
    db.close();return {'audit_run':audit_run,'status':overall,'events':result}
