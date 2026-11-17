"""Synthetic relation intake and manual functional-dependency repair evaluation.

This module checks a proposed table; it does not choose a repair.
"""

import hashlib
import json
from pathlib import Path

from .store import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS quality_source (
 source_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batch(id),
 digest TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS quality_manual_plan (
 plan_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES quality_source(source_id),
 digest TEXT NOT NULL, raw BLOB NOT NULL, result TEXT NOT NULL);
"""


def record(db_path, input_path):
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw)
    fields = doc['fields']
    if not doc['source_id'] or not fields or len(fields) != len(set(fields)) or not doc['rows']:
        raise ValueError('quality_empty_source')
    if not set(doc['immutable_fields']) <= set(fields):
        raise ValueError('quality_bad_lock')
    for field in fields:
        if not doc['domains'].get(field):
            raise ValueError('quality_empty_domain')
    ids = set()
    for row in doc['rows']:
        if not row['id'] or row['id'] in ids:
            raise ValueError('quality_duplicate_row')
        ids.add(row['id'])
        if set(row['values']) != set(fields):
            raise ValueError('quality_missing_field')
        for field in fields:
            if row['values'][field] not in doc['domains'][field]:
                raise ValueError('quality_outside_domain')
            weight = row['trust_weights'].get(field)
            if isinstance(weight, bool) or not isinstance(weight, int) or weight < 1:
                raise ValueError('quality_bad_trust_weight')
    for fd in doc['functional_dependencies']:
        if not fd['determinant'] or not set(fd['determinant'] + fd['dependent']) <= set(fields):
            raise ValueError('quality_bad_dependency')
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        batch = db.execute('SELECT config_raw FROM batch WHERE id=?',
                           (doc['batch_id'],)).fetchone()
        if not batch:
            raise ValueError('quality_unknown_batch')
        dataset = json.loads(batch['config_raw'])['dataset']
        if dataset['version'] != doc['dataset_version'] or set(fields) != set(dataset['fields']):
            raise ValueError('quality_dataset_mismatch')
        prior = db.execute('SELECT digest FROM quality_source WHERE source_id=?',
                           (doc['source_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('quality_source_conflict')
            return doc['source_id']
        with db:
            db.execute('INSERT INTO quality_source VALUES (?,?,?,?)',
                       (doc['source_id'], doc['batch_id'], digest, raw))
        return doc['source_id']
    finally:
        db.close()


def evaluate_manual(db_path, input_path):
    raw = Path(input_path).read_bytes()
    plan = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        prior = db.execute('SELECT digest,result FROM quality_manual_plan WHERE plan_id=?',
                           (plan['plan_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('quality_plan_conflict')
            return json.loads(prior['result'])
        row = db.execute('SELECT raw FROM quality_source WHERE source_id=?',
                         (plan['source_id'],)).fetchone()
        if not row:
            raise ValueError('quality_unknown_source')
        source = json.loads(row['raw'])
        original = {item['id']: item for item in source['rows']}
        proposed = {item['id']: item['values'] for item in plan['rows']}
        if len(proposed) != len(plan['rows']) or set(proposed) != set(original):
            raise ValueError('quality_not_same_rows')
        changes = []
        for row_id, values in proposed.items():
            if set(values) != set(source['fields']):
                raise ValueError('quality_missing_field')
            for field in source['fields']:
                if values[field] not in source['domains'][field]:
                    raise ValueError('quality_outside_domain')
                old = original[row_id]['values'][field]
                if values[field] != old:
                    if field in source['immutable_fields']:
                        raise ValueError('quality_locked_field')
                    changes.append({'row': row_id, 'field': field,
                                    'before': old, 'after': values[field],
                                    'weight': original[row_id]['trust_weights'][field]})
        violations = []
        for fd in source['functional_dependencies']:
            seen = {}
            for row_id, values in proposed.items():
                determinant = tuple(values[field] for field in fd['determinant'])
                dependent = tuple(values[field] for field in fd['dependent'])
                if determinant in seen and seen[determinant][0] != dependent:
                    violations.append({'determinant': fd['determinant'],
                                       'dependent': fd['dependent'],
                                       'rows': [seen[determinant][1], row_id]})
                else:
                    seen[determinant] = dependent, row_id
        result = {'source_id': plan['source_id'], 'plan_id': plan['plan_id'],
                  'valid': not violations, 'changed_cells': len(changes),
                  'trust_cost': sum(change['weight'] for change in changes),
                  'changes': changes, 'violations': violations}
        with db:
            db.execute('INSERT INTO quality_manual_plan VALUES (?,?,?,?,?)',
                       (plan['plan_id'], plan['source_id'], digest, raw,
                        json.dumps(result, sort_keys=True)))
        return result
    finally:
        db.close()
