"""Freeze synthetic decision aggregates and report their raw group rates.

The raw summary ignores case-mix strata; it is not a stratified diagnostic.
"""

import hashlib
import json
from fractions import Fraction
from pathlib import Path

from .store import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS group_audit_source (
 audit_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batch(id),
 digest TEXT NOT NULL, raw BLOB NOT NULL);
"""


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('group_audit_bad_count')
    return value


def record(db_path, input_path):
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw)
    if not doc['audit_id'] or len(doc['groups']) != 2 or not doc['strata']:
        raise ValueError('group_audit_bad_contract')
    if len(set(doc['groups'])) != 2 or len(set(doc['strata'])) != len(doc['strata']):
        raise ValueError('group_audit_duplicate_label')
    pairs = set()
    for cell in doc['cells']:
        pair = cell['group'], cell['stratum']
        if pair[0] not in doc['groups'] or pair[1] not in doc['strata'] or pair in pairs:
            raise ValueError('group_audit_bad_cell')
        pairs.add(pair)
        _count(cell['allowed'])
        _count(cell['denied'])
        if cell['allowed'] + cell['denied'] == 0:
            raise ValueError('group_audit_empty_cell')
    if pairs != {(g, s) for g in doc['groups'] for s in doc['strata']}:
        raise ValueError('group_audit_incomplete_matrix')
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        batch = db.execute('SELECT config_raw FROM batch WHERE id=?',
                           (doc['batch_id'],)).fetchone()
        if not batch:
            raise ValueError('group_audit_unknown_batch')
        config = json.loads(batch['config_raw'])
        if config['rule_version'] != doc['rule_version']:
            raise ValueError('group_audit_wrong_rule_version')
        prior = db.execute('SELECT digest FROM group_audit_source WHERE audit_id=?',
                           (doc['audit_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('group_audit_source_conflict')
            return doc['audit_id']
        with db:
            db.execute('INSERT INTO group_audit_source VALUES (?,?,?,?)',
                       (doc['audit_id'], doc['batch_id'], digest, raw))
        return doc['audit_id']
    finally:
        db.close()


def raw_summary(db_path, audit_id):
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        row = db.execute('SELECT raw FROM group_audit_source WHERE audit_id=?',
                         (audit_id,)).fetchone()
        if not row:
            raise ValueError('group_audit_unknown_source')
        doc = json.loads(row['raw'])
        result = {}
        for group in doc['groups']:
            cells = [c for c in doc['cells'] if c['group'] == group]
            allowed = sum(c['allowed'] for c in cells)
            total = sum(c['allowed'] + c['denied'] for c in cells)
            rate = Fraction(allowed, total)
            result[group] = {'allowed': allowed, 'total': total,
                             'rate_num': rate.numerator, 'rate_den': rate.denominator}
        a, b = doc['groups']
        gap = Fraction(result[a]['allowed'], result[a]['total']) - Fraction(
            result[b]['allowed'], result[b]['total'])
        return {'audit_id': audit_id, 'groups': result,
                'gap_num': gap.numerator, 'gap_den': gap.denominator}
    finally:
        db.close()
