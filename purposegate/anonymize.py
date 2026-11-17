"""Synthetic microtable intake and evaluation of an explicitly chosen grouping.

This workflow does not search for a minimum-loss privacy transformation.
"""

import hashlib
import json
from pathlib import Path

from .store import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS anon_source (
 source_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batch(id),
 digest TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS anon_manual_plan (
 plan_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES anon_source(source_id),
 digest TEXT NOT NULL, raw BLOB NOT NULL, result TEXT NOT NULL);
"""


def record(db_path, input_path):
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw)
    if not doc['source_id'] or not doc['rows'] or not doc['quasi_identifiers']:
        raise ValueError('anon_empty_source')
    row_ids = [row['id'] for row in doc['rows']]
    if len(row_ids) != len(set(row_ids)) or not all(row_ids):
        raise ValueError('anon_duplicate_row')
    for field in doc['quasi_identifiers']:
        spec = doc['hierarchies'][field]
        for row in doc['rows']:
            value = str(row[field])
            if value not in spec['ancestors']:
                raise ValueError('anon_missing_hierarchy_value')
            chain = spec['ancestors'][value]
            if not chain or chain[0] != value or any(node not in spec['costs'] for node in chain):
                raise ValueError('anon_bad_hierarchy')
    for row in doc['rows']:
        if doc['sensitive_field'] not in row:
            raise ValueError('anon_missing_sensitive_value')
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        batch = db.execute('SELECT config_raw FROM batch WHERE id=?',
                           (doc['batch_id'],)).fetchone()
        if not batch:
            raise ValueError('anon_unknown_batch')
        config = json.loads(batch['config_raw'])
        dataset = config['dataset']
        fields = set(dataset['fields'])
        if dataset['version'] != doc['dataset_version'] or not (
                set(doc['quasi_identifiers']) | {doc['sensitive_field']}) <= fields:
            raise ValueError('anon_dataset_mismatch')
        prior = db.execute('SELECT digest FROM anon_source WHERE source_id=?',
                           (doc['source_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('anon_source_conflict')
            return doc['source_id']
        with db:
            db.execute('INSERT INTO anon_source VALUES (?,?,?,?)',
                       (doc['source_id'], doc['batch_id'], digest, raw))
        return doc['source_id']
    finally:
        db.close()


def _generalize(values, spec):
    common = set(spec['ancestors'][str(values[0])])
    for value in values[1:]:
        common &= set(spec['ancestors'][str(value)])
    if not common:
        raise ValueError('anon_no_common_generalization')
    return min(common, key=lambda node: (spec['costs'][node], node))


def evaluate_manual(db_path, input_path):
    raw = Path(input_path).read_bytes()
    plan = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    k, diversity = plan['k'], plan['l_distinct']
    if any(isinstance(x, bool) or not isinstance(x, int) or x < 1 for x in (k, diversity)):
        raise ValueError('anon_bad_threshold')
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        prior = db.execute('SELECT digest,result FROM anon_manual_plan WHERE plan_id=?',
                           (plan['plan_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('anon_plan_conflict')
            return json.loads(prior['result'])
        source = db.execute('SELECT raw FROM anon_source WHERE source_id=?',
                            (plan['source_id'],)).fetchone()
        if not source:
            raise ValueError('anon_unknown_source')
        doc = json.loads(source['raw'])
        rows = {row['id']: row for row in doc['rows']}
        used = [row_id for group in plan['groups'] for row_id in group]
        if len(used) != len(set(used)) or set(used) != set(rows):
            raise ValueError('anon_not_a_partition')
        output = []
        loss = 0
        valid = True
        for group in plan['groups']:
            if not group:
                raise ValueError('anon_empty_group')
            generalized = {}
            for field in doc['quasi_identifiers']:
                spec = doc['hierarchies'][field]
                node = _generalize([rows[row_id][field] for row_id in group], spec)
                generalized[field] = node
                loss += len(group) * spec['costs'][node]
            distinct = len({str(rows[row_id][doc['sensitive_field']]) for row_id in group})
            if len(group) < k or distinct < diversity:
                valid = False
            output.append({'ids': sorted(group), 'qi': generalized,
                           'size': len(group), 'sensitive_distinct': distinct})
        result = {'source_id': plan['source_id'], 'plan_id': plan['plan_id'],
                  'valid': valid, 'k': k, 'l_distinct': diversity,
                  'loss': loss, 'groups': sorted(output, key=lambda item: item['ids'])}
        with db:
            db.execute('INSERT INTO anon_manual_plan VALUES (?,?,?,?,?)',
                       (plan['plan_id'], plan['source_id'], digest, raw,
                        json.dumps(result, sort_keys=True)))
        return result
    finally:
        db.close()
