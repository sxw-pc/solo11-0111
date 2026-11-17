import hashlib, json
from pathlib import Path
from .store import connect
from .timeline import CONTRACTS, DELEGATION_KINDS, instant


def hash_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


DELEGATION_REQUIRED = {
    'delegate_source': ('delegatee', 'purpose', 'fields', 'from', 'until', 'at'),
    'delegate': ('delegator', 'delegatee', 'purpose', 'fields', 'from', 'until', 'at'),
    'delegate_revoke': ('delegator', 'delegatee', 'at'),
}


def _delegation_problem(item):
    """Return a semantic issue code for a parsed delegation event, if any.

    The event is still stored: the chain resolver must be able to show the
    defective step in its evidence instead of silently dropping it.
    """
    kind = item['kind']
    required = DELEGATION_REQUIRED[kind]
    if any(item.get(name) in (None, '') for name in required): return 'delegation_incomplete'
    if kind == 'delegate_source':
        if item.get('revocation_effect') not in CONTRACTS: return 'delegation_contract_missing'
        if not isinstance(item.get('delegate_allowed'), bool): return 'delegation_flag_missing'
    if kind == 'delegate':
        if not isinstance(item.get('source_event_key'), str) and not isinstance(item.get('source'), dict):
            return 'delegation_source_missing'
    if kind in ('delegate_source', 'delegate'):
        if not isinstance(item.get('fields'), list) or not all(isinstance(f, str) for f in item['fields']):
            return 'delegation_incomplete'
        if instant(item['from']) >= instant(item['until']): return 'delegation_invalid_interval'
    return None


def initialize(db_path, config_path):
    raw = Path(config_path).read_bytes(); config = json.loads(raw); db = connect(db_path)
    bid = config['batch_id']; digest = hash_bytes(raw)
    with db:
        row = db.execute('SELECT config_hash FROM batch WHERE id=?', (bid,)).fetchone()
        if row and row['config_hash'] != digest: raise ValueError('batch_config_conflict')
        db.execute('INSERT OR IGNORE INTO batch VALUES (?,?,?)', (bid, digest, raw))
        for key in config['subjects']:
            db.execute('INSERT OR IGNORE INTO subject VALUES (?,?)', (key, bid))
        rule = config.get('rule_version', 'v1')
        db.execute('INSERT OR IGNORE INTO rule_version VALUES (?,?,?)', (rule, json.dumps(config, sort_keys=True), digest))
    db.close(); return bid


def load(db_path, path):
    raw = Path(path).read_bytes(); db = connect(db_path)
    batch = db.execute('SELECT id FROM batch LIMIT 1').fetchone()
    if not batch: raise ValueError('not_initialized')
    if db.execute('SELECT 1 FROM source_file WHERE batch_id=? AND digest=?', (batch['id'], hash_bytes(raw))).fetchone(): return 0
    count = 0
    with db:
        sid = db.execute('INSERT INTO source_file(batch_id,name,digest,raw) VALUES (?,?,?,?)', (batch['id'], str(path), hash_bytes(raw), raw)).lastrowid
        for line_no, line in enumerate(raw.decode('utf-8-sig').splitlines(), 1):
            try:
                item = json.loads(line); key = str(item['event_key']); subject = str(item['subject']); kind = str(item['kind'])
                if not db.execute('SELECT 1 FROM subject WHERE id=?', (subject,)).fetchone(): raise ValueError('unknown_subject')
                if kind not in {'grant', 'narrow', 'revoke'} | DELEGATION_KINDS: raise ValueError('unknown_event_kind')
                instant(item['at'])
                if kind == 'grant':
                    if instant(item['from']) >= instant(item['until']): raise ValueError('invalid_grant_interval')
                digest = hash_bytes(line.encode()); old = db.execute('SELECT digest FROM raw_event WHERE event_key=?', (key,)).fetchone()
                if old:
                    if old['digest'] != digest:
                        db.execute('INSERT INTO issue(source_id,line_no,subject,code,detail) VALUES (?,?,?,?,?)', (sid, line_no, subject, 'identity_conflict', key))
                    continue
                problem = _delegation_problem(item) if kind in DELEGATION_KINDS else None
                db.execute('INSERT INTO raw_event(source_id,line_no,event_key,subject,kind,at,payload,digest) VALUES (?,?,?,?,?,?,?,?)',
                           (sid, line_no, key, subject, kind, item.get('at'), json.dumps(item, ensure_ascii=False), digest)); count += 1
                if problem:
                    db.execute('INSERT INTO issue(source_id,line_no,subject,code,detail) VALUES (?,?,?,?,?)',
                               (sid, line_no, subject, problem, f'{key}:{line_no}'))
            except (ValueError, KeyError, TypeError) as exc:
                db.execute('INSERT INTO issue(source_id,line_no,code,detail) VALUES (?,?,?,?)', (sid, line_no, 'parse_error', str(exc)))
    db.close(); return count
