"""Intake for a synthetic multi-person derived dataset and correction notices.

The stored records are evidence only: no corrected values, ranks or permission
decisions are produced by this module.
"""

import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .store import connect


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
"""


def _score(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('bad_score') from exc
    if not number.is_finite() or number.as_tuple().exponent < -2:
        raise ValueError('bad_score')
    return str(number)


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
        db.executescript(SCHEMA)
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
        for key in ('event_key','cohort_id','person','field','old_value','new_value','at'):
            if not isinstance(item.get(key), str) or not item[key]:
                raise ValueError(f'correction_missing_{key}')
        if item['field'] != 'score':
            raise ValueError('unsupported_correction_field')
        _score(item['old_value']); _score(item['new_value'])
        parsed.append((line_no, line, item, hashlib.sha256(line).hexdigest()))
    db = connect(path)
    try:
        db.executescript(SCHEMA)
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
