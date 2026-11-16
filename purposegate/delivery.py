"""Version-bound materialization and release, downstream of authorization."""
import hashlib
import json
from pathlib import Path

from .store import connect

SCHEMA="""
CREATE TABLE IF NOT EXISTS delivery_package(
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL, run_id INTEGER NOT NULL REFERENCES run(id),
 manifest_hash TEXT NOT NULL, fields_json TEXT NOT NULL, values_hash TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('prepared','released','blocked')),
 released_at TEXT, output_path TEXT);
CREATE TABLE IF NOT EXISTS delivery_problem(
 package_id TEXT NOT NULL REFERENCES delivery_package(id), code TEXT NOT NULL,
 detail TEXT NOT NULL, PRIMARY KEY(package_id,code));
"""

def _canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()

def _digest(value): return hashlib.sha256(_canonical(value)).hexdigest()

def prepare(db_path, package_id, request_id, values):
    """Materialize exactly the fields authorized by a frozen evaluation."""
    db=connect(db_path);db.executescript(SCHEMA)
    run=db.execute('SELECT max(id) FROM run').fetchone()[0]
    decision=db.execute('SELECT * FROM decision WHERE run_id=? AND subject=?',(run,request_id)).fetchone()
    if not decision or decision['status']!='允许':raise ValueError('delivery_not_authorized')
    allowed=json.loads(decision['detail'])['allowed_fields']
    if set(values)!=set(allowed):raise ValueError('delivery_field_mismatch')
    config_hash=db.execute('SELECT config_hash FROM batch LIMIT 1').fetchone()[0]
    manifest={'request_id':request_id,'run_id':run,'fields':allowed,'config_hash':config_hash}
    manifest_hash=_digest(manifest);values_hash=_digest(values)
    old=db.execute('SELECT * FROM delivery_package WHERE id=?',(package_id,)).fetchone()
    if old:
        if old['manifest_hash']!=manifest_hash or old['values_hash']!=values_hash:raise ValueError('delivery_identity_conflict')
        return inspect(db_path,package_id)
    with db:
        db.execute('INSERT INTO delivery_package(id,request_id,run_id,manifest_hash,fields_json,values_hash,state) VALUES (?,?,?,?,?,?,?)',
                   (package_id,request_id,run,manifest_hash,json.dumps(allowed),values_hash,'prepared'))
    db.close();return inspect(db_path,package_id)

def release(db_path, package_id, values, destination):
    """The source run and exact materialization must still be current."""
    db=connect(db_path);db.executescript(SCHEMA)
    row=db.execute('SELECT * FROM delivery_package WHERE id=?',(package_id,)).fetchone()
    if not row:raise ValueError('unknown_delivery')
    if row['state']=='released':return inspect(db_path,package_id)
    if row['state']!='prepared':raise ValueError('delivery_not_prepared')
    current=db.execute('SELECT max(id) FROM run').fetchone()[0]
    decision=db.execute('SELECT * FROM decision WHERE run_id=? AND subject=?',(current,row['request_id'])).fetchone()
    if current!=row['run_id'] or not decision or decision['status']!='允许':raise ValueError('delivery_authorization_stale')
    if _digest(values)!=row['values_hash'] or set(values)!=set(json.loads(row['fields_json'])):
        raise ValueError('delivery_bytes_changed')
    dest=Path(destination)
    if dest.exists():raise ValueError('delivery_destination_exists')
    dest.parent.mkdir(parents=True,exist_ok=True)
    payload={'request_id':row['request_id'],'run_id':row['run_id'],'fields':values,'manifest_hash':row['manifest_hash']}
    temp=dest.with_name(dest.name+'.pending')
    temp.write_bytes(_canonical(payload))
    try:
        with db:db.execute("UPDATE delivery_package SET state='released',released_at=CURRENT_TIMESTAMP,output_path=? WHERE id=?",
                           (str(dest),package_id))
        temp.replace(dest)
    except Exception:
        temp.unlink(missing_ok=True)
        with db:db.execute("UPDATE delivery_package SET state='blocked' WHERE id=?",(package_id,))
        raise
    db.close();return inspect(db_path,package_id)

def inspect(db_path,package_id):
    db=connect(db_path);db.executescript(SCHEMA)
    row=db.execute('SELECT * FROM delivery_package WHERE id=?',(package_id,)).fetchone()
    if not row:raise ValueError('unknown_delivery')
    result=dict(row);db.close();return result
