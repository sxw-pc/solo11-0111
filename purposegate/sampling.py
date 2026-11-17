"""Freeze a synthetic data-product frame and evaluate a manual sample allocation.

This module does not choose an optimal allocation.
"""

import hashlib
import json
from fractions import Fraction
from pathlib import Path

from .store import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS sample_frame (
 frame_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batch(id),
 digest TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS sample_manual_plan (
 plan_id TEXT PRIMARY KEY, frame_id TEXT NOT NULL REFERENCES sample_frame(frame_id),
 digest TEXT NOT NULL, raw BLOB NOT NULL, result TEXT NOT NULL);
"""


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('sample_bad_positive_integer')
    return value


def record_frame(db_path, input_path):
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw)
    if not doc['frame_id'] or not doc['strata'] or not doc['rows']:
        raise ValueError('sample_empty_frame')
    _positive(doc['budget'])
    strata = {}
    for spec in doc['strata']:
        if not spec['id'] or spec['id'] in strata:
            raise ValueError('sample_duplicate_stratum')
        _positive(spec['cost_per_row'])
        _positive(spec['pilot_variance_num'])
        _positive(spec['pilot_variance_den'])
        strata[spec['id']] = spec
    seen = set()
    counts = {key: 0 for key in strata}
    for row in doc['rows']:
        if not row['id'] or row['id'] in seen or row['stratum'] not in strata:
            raise ValueError('sample_bad_row')
        seen.add(row['id'])
        counts[row['stratum']] += 1
    if any(count == 0 for count in counts.values()):
        raise ValueError('sample_empty_stratum')
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        batch = db.execute('SELECT config_raw FROM batch WHERE id=?',
                           (doc['batch_id'],)).fetchone()
        if not batch:
            raise ValueError('sample_unknown_batch')
        dataset = json.loads(batch['config_raw'])['dataset']
        if dataset['version'] != doc['dataset_version'] or doc['value_field'] not in dataset['fields']:
            raise ValueError('sample_dataset_mismatch')
        prior = db.execute('SELECT digest FROM sample_frame WHERE frame_id=?',
                           (doc['frame_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('sample_frame_conflict')
            return doc['frame_id']
        with db:
            db.execute('INSERT INTO sample_frame VALUES (?,?,?,?)',
                       (doc['frame_id'], doc['batch_id'], digest, raw))
        return doc['frame_id']
    finally:
        db.close()


def evaluate_manual(db_path, input_path):
    raw = Path(input_path).read_bytes()
    plan = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        prior = db.execute('SELECT digest,result FROM sample_manual_plan WHERE plan_id=?',
                           (plan['plan_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('sample_plan_conflict')
            return json.loads(prior['result'])
        row = db.execute('SELECT raw FROM sample_frame WHERE frame_id=?',
                         (plan['frame_id'],)).fetchone()
        if not row:
            raise ValueError('sample_unknown_frame')
        frame = json.loads(row['raw'])
        specs = {spec['id']: spec for spec in frame['strata']}
        if set(plan['allocation']) != set(specs):
            raise ValueError('sample_allocation_missing_stratum')
        budget = 0
        selected = {}
        total_n = len(frame['rows'])
        variance = Fraction(0)
        coverages = []
        for stratum, spec in specs.items():
            n = _positive(plan['allocation'][stratum])
            members = [item['id'] for item in frame['rows'] if item['stratum'] == stratum]
            population = len(members)
            if n > population:
                raise ValueError('sample_allocation_exceeds_frame')
            budget += n * spec['cost_per_row']
            ordered = sorted(members, key=lambda member: (
                hashlib.sha256((frame['priority_seed'] + '\0' + member).encode()).hexdigest(), member))
            inclusion = Fraction(n, population)
            selected[stratum] = {'ids': ordered[:n], 'n': n, 'N': population,
                                 'inclusion_num': inclusion.numerator,
                                 'inclusion_den': inclusion.denominator}
            weight = Fraction(population, total_n)
            pilot = Fraction(spec['pilot_variance_num'], spec['pilot_variance_den'])
            variance += weight * weight * pilot * Fraction(population - n, population * n)
            coverages.append(inclusion)
        feasible = budget <= frame['budget']
        gap = max(coverages) - min(coverages)
        result = {'frame_id': plan['frame_id'], 'plan_id': plan['plan_id'],
                  'feasible': feasible, 'spent': budget, 'selected': selected,
                  'variance_num': variance.numerator, 'variance_den': variance.denominator,
                  'coverage_gap_num': gap.numerator, 'coverage_gap_den': gap.denominator}
        with db:
            db.execute('INSERT INTO sample_manual_plan VALUES (?,?,?,?,?)',
                       (plan['plan_id'], plan['frame_id'], digest, raw,
                        json.dumps(result, sort_keys=True)))
        return result
    finally:
        db.close()
