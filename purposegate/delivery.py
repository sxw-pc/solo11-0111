"""Version-bound materialization and a reviewable, crash-safe release protocol.

A release is not finished when a row says ``released``: the protocol freezes the
package identity, authorization run, exact value digest, destination path and
the digest of the intended temporary file before writing anything.  Every later
step (write temp, commit ledger, rename, final confirmation) is durable on its
own, so a process that dies between any two windows leaves enough evidence for
``recover`` to classify the situation instead of guessing from the state string.
"""
import hashlib
import json
import os
from pathlib import Path

from .store import add_columns, connect

SCHEMA="""
CREATE TABLE IF NOT EXISTS delivery_package(
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL, run_id INTEGER NOT NULL REFERENCES run(id),
 manifest_hash TEXT NOT NULL, fields_json TEXT NOT NULL, values_hash TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('prepared','released','blocked')),
 released_at TEXT, output_path TEXT);
CREATE TABLE IF NOT EXISTS delivery_problem(
 package_id TEXT NOT NULL REFERENCES delivery_package(id), code TEXT NOT NULL,
 detail TEXT NOT NULL, PRIMARY KEY(package_id,code));
CREATE TABLE IF NOT EXISTS delivery_release(
 package_id TEXT NOT NULL REFERENCES delivery_package(id), attempt INTEGER NOT NULL,
 request_id TEXT NOT NULL, run_id INTEGER NOT NULL, manifest_hash TEXT NOT NULL,
 values_hash TEXT NOT NULL, destination TEXT NOT NULL, temp_path TEXT NOT NULL,
 payload_digest TEXT NOT NULL, temp_digest TEXT NOT NULL, destination_digest TEXT,
 status TEXT NOT NULL CHECK(status IN ('prepared','committed','delivered','reapproval_required','failed')),
 prepared_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, committed_at TEXT, delivered_at TEXT,
 PRIMARY KEY(package_id,attempt));
CREATE TABLE IF NOT EXISTS delivery_recovery(
 id INTEGER PRIMARY KEY, package_id TEXT NOT NULL REFERENCES delivery_package(id),
 route TEXT NOT NULL, outcome TEXT NOT NULL, reason TEXT NOT NULL,
 evidence TEXT NOT NULL, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""

# Forward migration for ledgers created by the earlier release implementation.
_PACKAGE_COLUMNS=(('output_digest','TEXT'),('release_status','TEXT'))

FAULT_WINDOWS=('prepare','temp','commit','rename','confirm')
ROUTES=('promote_verified','require_reapproval')

OUTCOME_DELIVERED='delivered'
OUTCOME_REAPPROVAL='reapproval_required'
OUTCOME_EVIDENCE_INCOMPLETE='evidence_incomplete'
OUTCOME_CONTENT_CONFLICT='content_conflict'
OUTCOME_AUTHORIZATION_STALE='authorization_stale'

# Tests (and an operator replaying a failure) may install a hook.  A crash is a
# BaseException so ordinary cleanup code cannot swallow it: the process really
# stops at the requested window.  ``PURPOSEGATE_FAULT_AT`` does the same for the
# command line.
FAULT_HOOK=None


class InjectedCrash(BaseException):
    """Simulated abrupt termination at a named release window."""

    def __init__(self,window):
        self.window=window
        super().__init__(f'injected_crash:{window}')


def _fault(window):
    hook=FAULT_HOOK
    if hook is not None:
        hook(window)
    elif os.environ.get('PURPOSEGATE_FAULT_AT')==window:
        raise InjectedCrash(window)


def _canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()

def _digest(value): return hashlib.sha256(_canonical(value)).hexdigest()

def _file_digest(path):
    """Stream a file digest; ``None`` when the file is absent."""
    try:
        with open(path,'rb') as handle:
            digest=hashlib.sha256()
            for chunk in iter(lambda:handle.read(65536),b''):
                digest.update(chunk)
            return digest.hexdigest()
    except (FileNotFoundError,IsADirectoryError,NotADirectoryError):
        return None

def _absolute(path): return Path(os.path.abspath(os.fspath(path)))

def _fsync_dir(path):
    try:
        fd=os.open(os.fspath(path),os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)

def _publish_noreplace(temp,destination):
    """Atomically publish ``temp`` without ever overwriting an existing file.

    A hard link is created first because link(2) fails with EEXIST instead of
    replacing the destination, closing the check-then-rename race.  On file
    systems without hard links we fall back to a guarded rename.
    """
    temp=Path(temp);destination=Path(destination)
    try:
        os.link(temp,destination)
    except FileExistsError:
        return False
    except OSError:
        if destination.exists():return False
        os.replace(temp,destination)
        return True
    try:
        temp.unlink()
    except FileNotFoundError:
        pass
    return True

def _discardable(release_row):
    """True when a ``prepared`` attempt provably wrote nothing to disk.

    Only then may a retry replace its intent: any existing pending file (even an
    empty or truncated one) is evidence for ``recover`` to classify.
    """
    return (release_row['status']=='prepared'
            and _file_digest(release_row['temp_path']) is None
            and _file_digest(release_row['destination']) is None)

def ensure_schema(db):
    """Create protocol tables and add columns to a ledger from an older build."""
    db.executescript(SCHEMA)
    add_columns(db,'delivery_package',_PACKAGE_COLUMNS)
    _migrate_legacy_releases(db)
    db.commit()

def _migrate_legacy_releases(db):
    """Backfill proof rows for deliveries made by the pre-protocol release.

    A legacy row only becomes ``delivered`` evidence when its output file is
    still present and independently recomputes to the frozen package hashes.
    A missing or conflicting file is left without a release row, so later use
    stays uncertain instead of being trusted because of a state string.
    """
    rows=db.execute("SELECT p.* FROM delivery_package p LEFT JOIN delivery_release r "
                    "ON r.package_id=p.id WHERE p.state='released' AND r.package_id IS NULL").fetchall()
    for row in rows:
        if not row['output_path']:continue
        destination=Path(row['output_path'])
        digest=_file_digest(destination)
        if digest is None:continue
        try:payload=json.loads(destination.read_text(encoding='utf-8'))
        except (OSError,ValueError,UnicodeError):continue
        if not isinstance(payload,dict):continue
        if payload.get('manifest_hash')!=row['manifest_hash']:continue
        if payload.get('request_id')!=row['request_id'] or payload.get('run_id')!=row['run_id']:continue
        fields=payload.get('fields')
        if not isinstance(fields,dict) or _digest(fields)!=row['values_hash']:continue
        if set(fields)!=set(json.loads(row['fields_json'])):continue
        with db:
            db.execute('INSERT OR IGNORE INTO delivery_release(package_id,request_id,run_id,manifest_hash,'
                       'values_hash,destination,temp_path,payload_digest,temp_digest,destination_digest,'
                       'status,attempt,prepared_at,committed_at,delivered_at) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,COALESCE(?,CURRENT_TIMESTAMP),'
                       'COALESCE(?,CURRENT_TIMESTAMP),COALESCE(?,CURRENT_TIMESTAMP))',
                       (row['id'],row['request_id'],row['run_id'],row['manifest_hash'],row['values_hash'],
                        str(destination),str(destination)+'.pending',digest,digest,digest,'delivered',1,
                        row['released_at'],row['released_at'],row['released_at']))
            db.execute("UPDATE delivery_package SET release_status='delivered',output_digest=? WHERE id=?",
                       (digest,row['id']))

def _release_row(db,package_id):
    """The current (highest-attempt) release intent for a package."""
    return db.execute('SELECT * FROM delivery_release WHERE package_id=? '
                      'ORDER BY attempt DESC LIMIT 1',(package_id,)).fetchone()

def _package(db,package_id):
    return db.execute('SELECT * FROM delivery_package WHERE id=?',(package_id,)).fetchone()

def _ledger_mismatch(package,release):
    """Identity fields frozen in the release row must still match the package."""
    for key in ('request_id','run_id','manifest_hash','values_hash'):
        if release[key]!=package[key]: return f'ledger_{key}_mismatch'
    return None

def _authorization(db,release):
    current=db.execute('SELECT max(id) FROM run').fetchone()[0]
    decision=db.execute('SELECT status FROM decision WHERE run_id=? AND subject=?',
                        (release['run_id'],release['request_id'])).fetchone()
    status=decision['status'] if decision else None
    return {'current_run':current,'run_id':release['run_id'],
            'decision_status':status,
            'valid':current==release['run_id'] and status=='允许'}

def prepare(db_path, package_id, request_id, values):
    """Materialize exactly the fields authorized by a frozen evaluation."""
    db=connect(db_path);ensure_schema(db)
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
        unresolved=old['release_status'] in ('prepared','committed')
        if unresolved:
            db.close();raise ValueError('delivery_recovery_required')
        same_values=old['values_hash']==values_hash
        reapproval=(old['state']=='blocked' and old['release_status']=='reapproval_required')
        if same_values and reapproval:
            # A fresh authorization supersedes the quarantined attempt.  The
            # recovery history stays; only the pending release intent is
            # replaced by the new frozen identity.
            with db:
                db.execute('UPDATE delivery_package SET run_id=?,manifest_hash=?,fields_json=?,'
                           "state='prepared',release_status=NULL,released_at=NULL,"
                           'output_path=NULL,output_digest=NULL WHERE id=?',
                           (run,manifest_hash,json.dumps(allowed),package_id))
            db.close();return inspect(db_path,package_id)
        if old['manifest_hash']!=manifest_hash or not same_values:raise ValueError('delivery_identity_conflict')
        db.close();return inspect(db_path,package_id)
    with db:
        db.execute('INSERT INTO delivery_package(id,request_id,run_id,manifest_hash,fields_json,values_hash,state) VALUES (?,?,?,?,?,?,?)',
                   (package_id,request_id,run,manifest_hash,json.dumps(allowed),values_hash,'prepared'))
    db.close();return inspect(db_path,package_id)

def _payload_bytes(row, values):
    return _canonical({'request_id':row['request_id'],'run_id':row['run_id'],
                       'fields':values,'manifest_hash':row['manifest_hash']})

def _freeze_release(db,package,values,destination):
    """Commit the release intent before any file is written."""
    destination=_absolute(destination)
    temp=destination.with_name(destination.name+'.pending')
    if temp.exists():raise ValueError('delivery_recovery_required')
    payload=_payload_bytes(package,values)
    payload_digest=hashlib.sha256(payload).hexdigest()
    previous=db.execute('SELECT max(attempt) FROM delivery_release WHERE package_id=?',
                        (package['id'],)).fetchone()[0] or 0
    with db:
        db.execute('INSERT INTO delivery_release(package_id,attempt,request_id,run_id,manifest_hash,'
                   'values_hash,destination,temp_path,payload_digest,temp_digest,status) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                   (package['id'],previous+1,package['request_id'],package['run_id'],package['manifest_hash'],
                    package['values_hash'],str(destination),str(temp),payload_digest,payload_digest,'prepared'))
        db.execute("UPDATE delivery_package SET release_status='prepared' WHERE id=?",(package['id'],))
    return destination,temp,payload

def release(db_path, package_id, values, destination):
    """Release a frozen package through the reviewable protocol.

    ``released`` in the ledger only means the commit window was reached.  The
    destination is authoritative only after the final confirmation (or after
    ``recover`` verifies it).
    """
    db=connect(db_path);ensure_schema(db)
    row=_package(db,package_id)
    if not row:db.close();raise ValueError('unknown_delivery')
    destination=_absolute(destination)
    release_row=_release_row(db,package_id)
    if row['state']=='released':
        if release_row and release_row['status']=='delivered':
            if Path(release_row['destination'])!=destination:
                db.close();raise ValueError('delivery_destination_conflict')
            ok,_=verify_delivered(db,row,release_row)
            db.close()
            if ok:return inspect(db_path,package_id)
        db.close();raise ValueError('delivery_recovery_required')
    if release_row and release_row['status']=='reapproval_required':
        if row['state']=='prepared' and not row['release_status']:
            # A fresh prepare after reapproval supersedes the quarantined
            # attempt; its recovery history and quarantine file are kept.
            release_row=None
        else:
            db.close();raise ValueError('delivery_reapproval_required')
    if release_row and release_row['status']=='prepared' and _discardable(release_row):
        # The previous attempt died before any byte reached disk (or the
        # destination): its intent is closed, never its evidence of a written
        # file.  The row stays for audit and the attempt number keeps growing.
        with db:
            db.execute("UPDATE delivery_release SET status='failed' WHERE package_id=? AND attempt=?",
                       (package_id,release_row['attempt']))
        release_row=None
    if release_row:
        # An intent from an interrupted attempt: never overwrite its evidence.
        # ``recover`` decides whether the pending file may be promoted.
        db.close();raise ValueError('delivery_recovery_required')
    if row['state']!='prepared':db.close();raise ValueError('delivery_not_prepared')
    current=db.execute('SELECT max(id) FROM run').fetchone()[0]
    decision=db.execute('SELECT * FROM decision WHERE run_id=? AND subject=?',(current,row['request_id'])).fetchone()
    if current!=row['run_id'] or not decision or decision['status']!='允许':
        db.close();raise ValueError('delivery_authorization_stale')
    if _digest(values)!=row['values_hash'] or set(values)!=set(json.loads(row['fields_json'])):
        db.close();raise ValueError('delivery_bytes_changed')
    if destination.exists():
        db.close();raise ValueError('delivery_destination_exists')

    destination,temp,payload=_freeze_release(db,row,values,destination)
    release_row=_release_row(db,package_id)
    _fault('prepare')
    try:
        destination.parent.mkdir(parents=True,exist_ok=True)
        with open(temp,'xb') as handle:
            handle.write(payload);handle.flush();os.fsync(handle.fileno())
        _fsync_dir(destination.parent)
        _fault('temp')
        with db:
            db.execute("UPDATE delivery_release SET status='committed',committed_at=CURRENT_TIMESTAMP "
                       "WHERE package_id=? AND attempt=?",(package_id,release_row['attempt']))
            db.execute("UPDATE delivery_package SET state='released',release_status='committed',"
                       "released_at=CURRENT_TIMESTAMP,output_path=? WHERE id=?",(str(destination),package_id))
        _fault('commit')
        if not _publish_noreplace(temp,destination):
            raise ValueError('delivery_destination_exists')
        _fsync_dir(destination.parent)
        _fault('rename')
        delivered_digest=_file_digest(destination)
        if delivered_digest!=hashlib.sha256(payload).hexdigest():
            # Never delete what is at the destination: it may have been written
            # by someone else after our publish.  ``recover`` classifies it.
            raise ValueError('delivery_destination_content_conflict')
        with db:
            db.execute("UPDATE delivery_release SET status='delivered',delivered_at=CURRENT_TIMESTAMP,"
                       "destination_digest=? WHERE package_id=? AND attempt=?",
                       (delivered_digest,package_id,release_row['attempt']))
            db.execute("UPDATE delivery_package SET state='released',release_status='delivered',"
                       "output_digest=? WHERE id=?",(delivered_digest,package_id))
        _fault('confirm')
    except BaseException:
        # Keep the frozen release row and the pending file exactly as the OS
        # left them; the ledger never claims more than the completed windows.
        # ``recover`` is the only place allowed to resolve the situation.
        db.close();raise
    db.close();return inspect(db_path,package_id)

def verify_delivered(db,package,release_row=None):
    """Recompute the proof that the destination is this package's delivery.

    The ledger status alone is never accepted: the file bytes must match the
    frozen payload digest and the content must recompute to the package hashes.
    """
    if package is None:return False,'release_evidence_absent'
    if release_row is None:release_row=_release_row(db,package['id'])
    if release_row is None:return False,'release_evidence_absent'
    if release_row['status']!='delivered':return False,'release_not_delivered'
    mismatch=_ledger_mismatch(package,release_row)
    if mismatch:return False,mismatch
    if package['state']!='released':return False,'package_not_released'
    if package['output_path'] and Path(package['output_path'])!=Path(release_row['destination']):
        return False,'output_path_mismatch'
    destination=Path(release_row['destination'])
    digest=_file_digest(destination)
    if digest is None:return False,'destination_missing'
    if digest!=release_row['destination_digest']:return False,'destination_digest_mismatch'
    if digest!=release_row['payload_digest']:return False,'destination_content_mismatch'
    try:
        payload=json.loads(destination.read_text(encoding='utf-8'))
    except (OSError,ValueError,UnicodeError):
        return False,'destination_unreadable'
    if not isinstance(payload,dict):return False,'destination_unreadable'
    if payload.get('manifest_hash')!=package['manifest_hash']:return False,'manifest_mismatch'
    if payload.get('request_id')!=package['request_id'] or payload.get('run_id')!=package['run_id']:
        return False,'identity_mismatch'
    fields=payload.get('fields')
    if not isinstance(fields,dict) or _digest(fields)!=package['values_hash']:
        return False,'values_mismatch'
    if set(fields)!=set(json.loads(package['fields_json'])):return False,'fields_mismatch'
    return True,'release_proof_verified'

def _validate_file(release_row,package,path,label):
    """A recoverable file is complete, digest-identical and recomputes."""
    path=Path(path)
    digest=_file_digest(path)
    if digest is None:return False,f'{label}_missing',None
    if digest!=release_row['temp_digest']:return False,f'{label}_digest_mismatch',None
    if digest!=release_row['payload_digest']:return False,f'{label}_payload_mismatch',None
    try:
        payload=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError,UnicodeError):
        return False,f'{label}_unreadable',None
    if not isinstance(payload,dict):return False,f'{label}_unreadable',None
    if payload.get('request_id')!=release_row['request_id']:return False,f'{label}_identity_mismatch',None
    if payload.get('run_id')!=release_row['run_id']:return False,f'{label}_identity_mismatch',None
    if payload.get('manifest_hash')!=release_row['manifest_hash']:return False,f'{label}_manifest_mismatch',None
    fields=payload.get('fields')
    if not isinstance(fields,dict) or _digest(fields)!=release_row['values_hash']:
        return False,f'{label}_values_mismatch',None
    if set(fields)!=set(json.loads(package['fields_json'])):return False,f'{label}_fields_mismatch',None
    return True,f'{label}_verified',payload

def _validate_temp(release_row,package):
    return _validate_file(release_row,package,release_row['temp_path'],'temp')

def _validate_quarantine(release_row,package):
    return _validate_file(release_row,package,str(release_row['temp_path'])+'.quarantine','quarantine')

def _evidence(db,package,release_row):
    destination=Path(release_row['destination']) if release_row else None
    temp=Path(release_row['temp_path']) if release_row else None
    evidence={
        'package':{'id':package['id'],'request_id':package['request_id'],'run_id':package['run_id'],
                   'state':package['state'],'manifest_hash':package['manifest_hash'],
                   'values_hash':package['values_hash'],'output_path':package['output_path'],
                   'output_digest':package['output_digest']},
        'ledger':dict(release_row) if release_row else None,
        'files':{'destination':str(destination) if destination else None,
                 'destination_exists':bool(destination and destination.is_file()),
                 'destination_digest':_file_digest(destination) if destination else None,
                 'temp':str(temp) if temp else None,
                 'temp_exists':bool(temp and temp.is_file()),
                 'temp_digest':_file_digest(temp) if temp else None,
                 'quarantine':str(temp)+'.quarantine' if temp else None,
                 'quarantine_exists':bool(temp and Path(str(temp)+'.quarantine').is_file()),
                 'quarantine_digest':_file_digest(str(temp)+'.quarantine') if temp else None},
        'authorization':_authorization(db,release_row) if release_row else None,
    }
    return evidence

def _record(db,package_id,route,outcome,reason,evidence,changed):
    previous=db.execute('SELECT route,outcome FROM delivery_recovery WHERE package_id=? '
                        'ORDER BY id DESC LIMIT 1',(package_id,)).fetchone()
    # Re-running a recovery that mutates nothing appends nothing: repeated
    # invocations stay idempotent.  A different route that reaches the same
    # outcome is a distinct decision and is recorded.
    if previous and previous['route']==route and previous['outcome']==outcome and not changed:
        return False
    with db:
        db.execute('INSERT INTO delivery_recovery(package_id,route,outcome,reason,evidence) VALUES (?,?,?,?,?)',
                   (package_id,route,outcome,reason,
                    json.dumps(evidence,sort_keys=True,ensure_ascii=False)))
    return True

def _result(package,release_row,route,outcome,reason,evidence,changed,recorded):
    return {'package_id':package['id'],'route':route,'outcome':outcome,'reason':reason,
            'state':package['state'],
            'release_status':release_row['status'] if release_row else package['release_status'],
            'delivered':outcome==OUTCOME_DELIVERED,'changed':changed,'recorded':recorded,
            'destination':release_row['destination'] if release_row else None,
            'evidence':evidence}

def _finalize_delivered(db,package,release_row,destination_digest):
    """Commit a delivery whose destination bytes are already proven correct."""
    with db:
        db.execute("UPDATE delivery_release SET status='delivered',destination_digest=?,"
                   "delivered_at=CURRENT_TIMESTAMP WHERE package_id=? AND attempt=?",
                   (destination_digest,package['id'],release_row['attempt']))
        db.execute("UPDATE delivery_package SET state='released',release_status='delivered',"
                   "output_path=?,output_digest=?,released_at=CURRENT_TIMESTAMP WHERE id=?",
                   (release_row['destination'],destination_digest,package['id']))

def _promote(db,package,release_row):
    """Atomically rename the verified pending file and confirm the delivery."""
    temp=Path(release_row['temp_path']);destination=Path(release_row['destination'])
    existing=_file_digest(destination)
    if existing is not None:
        if existing!=release_row['payload_digest']:
            return OUTCOME_CONTENT_CONFLICT,'destination_content_conflict'
        _finalize_delivered(db,package,release_row,existing)
        return OUTCOME_DELIVERED,'destination_already_identical'
    destination.parent.mkdir(parents=True,exist_ok=True)
    if not _publish_noreplace(temp,destination):
        # Someone created the destination after our check; never overwrite it.
        existing=_file_digest(destination)
        if existing is None:
            return OUTCOME_EVIDENCE_INCOMPLETE,'destination_vanished'
        if existing!=release_row['payload_digest']:
            return OUTCOME_CONTENT_CONFLICT,'destination_content_conflict'
    _fsync_dir(destination.parent)
    digest=_file_digest(destination)
    if digest!=release_row['payload_digest']:
        return OUTCOME_CONTENT_CONFLICT,'destination_digest_mismatch'
    _finalize_delivered(db,package,release_row,digest)
    return OUTCOME_DELIVERED,'promoted_verified'

def _mark_reapproval(db,package,release_row):
    """Return the package to a state that needs a fresh authorization."""
    with db:
        db.execute("UPDATE delivery_release SET status='reapproval_required' WHERE package_id=? AND attempt=?",
                   (package['id'],release_row['attempt']))
        db.execute("UPDATE delivery_package SET state='blocked',release_status='reapproval_required',"
                   "output_path=NULL,output_digest=NULL WHERE id=?",(package['id'],))

def _quarantine(db,package,release_row,temp_ok):
    """Move the pending file aside and mark the package as needing reapproval.

    An incomplete or missing pending file cannot be promoted, but it must not
    leave the package looking delivered: the ledger still moves to the
    reapproval state and the outcome reports that the evidence was incomplete.
    """
    temp=Path(release_row['temp_path']);quarantine=Path(str(temp)+'.quarantine')
    if temp.exists():
        existing=_file_digest(quarantine)
        if existing is not None and existing!=release_row['temp_digest']:
            return OUTCOME_CONTENT_CONFLICT,'quarantine_content_conflict'
        os.replace(temp,quarantine)
        _fsync_dir(quarantine.parent)
        digest=_file_digest(quarantine)
        _mark_reapproval(db,package,release_row)
        if not temp_ok or digest!=release_row['temp_digest']:
            return OUTCOME_EVIDENCE_INCOMPLETE,'quarantined_incomplete_temp'
        return OUTCOME_REAPPROVAL,'quarantined_for_reapproval'
    digest=_file_digest(quarantine)
    _mark_reapproval(db,package,release_row)
    if digest is None:return OUTCOME_EVIDENCE_INCOMPLETE,'temp_missing'
    if digest!=release_row['temp_digest']:return OUTCOME_CONTENT_CONFLICT,'quarantine_digest_mismatch'
    return OUTCOME_REAPPROVAL,'already_quarantined' if temp_ok else 'quarantined_incomplete_temp'

def recover(db_path, package_id, route):
    """Classify one interrupted release and apply one of the safe routes.

    ``promote_verified`` finishes a verified pending delivery; it never
    overwrites a conflicting destination and never promotes without a current
    authorization.  ``require_reapproval`` quarantines the same pending file and
    returns the package to a state that needs a fresh authorization; it does not
    release anything automatically.  A destination that already contains the
    exact frozen payload is confirmed as delivered by both routes, because the
    rename window has already completed.
    """
    if route not in ROUTES:raise ValueError('unknown_recovery_route')
    db=connect(db_path);ensure_schema(db)
    package=_package(db,package_id)
    if not package:db.close();raise ValueError('unknown_delivery')
    release_row=_release_row(db,package_id)
    evidence=_evidence(db,package,release_row)
    before=(package['state'],package['release_status'],release_row['status'] if release_row else None)
    outcome=reason=None
    if release_row is None:
        outcome,reason=OUTCOME_EVIDENCE_INCOMPLETE,'release_evidence_absent'
    else:
        mismatch=_ledger_mismatch(package,release_row)
        if mismatch:
            outcome,reason=OUTCOME_EVIDENCE_INCOMPLETE,mismatch
        elif release_row['status']=='delivered':
            ok,why=verify_delivered(db,package,release_row)
            if ok:outcome,reason=OUTCOME_DELIVERED,'already_delivered'
            elif why in ('destination_missing',):
                outcome,reason=OUTCOME_EVIDENCE_INCOMPLETE,why
            else:
                outcome,reason=OUTCOME_CONTENT_CONFLICT,why
        elif release_row['status']=='failed':
            # A handled failure before publish: the pending evidence is no
            # longer trusted and the package needs a fresh review.
            outcome,reason=OUTCOME_EVIDENCE_INCOMPLETE,'release_interrupted_before_publish'
        elif release_row['status']=='reapproval_required':
            quarantine=Path(str(release_row['temp_path'])+'.quarantine')
            if quarantine.is_file():
                ok,why,_=_validate_quarantine(release_row,package)
                if ok:
                    outcome,reason=OUTCOME_REAPPROVAL,'already_quarantined'
                elif why=='quarantine_digest_mismatch':
                    outcome,reason=OUTCOME_CONTENT_CONFLICT,why
                else:
                    outcome,reason=OUTCOME_EVIDENCE_INCOMPLETE,why
            else:
                outcome,reason=OUTCOME_EVIDENCE_INCOMPLETE,'quarantine_missing'
        else:
            destination=Path(release_row['destination'])
            destination_digest=_file_digest(destination)
            if destination_digest is not None:
                # The rename window completed; confirm it instead of rolling back.
                if destination_digest==release_row['payload_digest']:
                    _finalize_delivered(db,package,release_row,destination_digest)
                    outcome,reason=OUTCOME_DELIVERED,'destination_already_identical'
                else:
                    outcome,reason=OUTCOME_CONTENT_CONFLICT,'destination_content_conflict'
            else:
                temp_ok,why,_=_validate_temp(release_row,package)
                authorization=_authorization(db,release_row)
                if route=='promote_verified':
                    if not temp_ok:
                        outcome,reason=OUTCOME_EVIDENCE_INCOMPLETE,why
                    elif not authorization['valid']:
                        outcome,reason=OUTCOME_AUTHORIZATION_STALE,'authorization_no_longer_valid'
                    else:
                        outcome,reason=_promote(db,package,release_row)
                else:
                    outcome,reason=_quarantine(db,package,release_row,temp_ok)
                    if not authorization['valid'] and temp_ok:
                        reason='quarantined_authorization_stale'
    # Re-read mutated rows so the returned state is the durable one.
    package=_package(db,package_id);release_row=_release_row(db,package_id)
    after=(package['state'],package['release_status'],release_row['status'] if release_row else None)
    changed=before!=after
    evidence=_evidence(db,package,release_row)
    recorded=_record(db,package_id,route,outcome,reason,evidence,changed)
    result=_result(package,release_row,route,outcome,reason,evidence,changed,recorded)
    db.close();return result

def inspect(db_path,package_id):
    db=connect(db_path);ensure_schema(db)
    row=_package(db,package_id)
    if not row:db.close();raise ValueError('unknown_delivery')
    release_row=_release_row(db,package_id)
    result=dict(row)
    result['release']=dict(release_row) if release_row else None
    if release_row and release_row['status']=='delivered':
        ok,reason=verify_delivered(db,row,release_row)
    else:
        ok,reason=False,'release_not_delivered' if release_row else 'release_evidence_absent'
    result['delivery_evidence']={'ok':ok,'reason':reason}
    db.close();return result
