"""Synthetic two-provider candidate-link intake and manual mapping evaluation.

The evaluator checks a supplied one-to-one mapping; it does not choose one.
It does not change purpose authorization or create a joint data product.
"""

import hashlib
import json
from pathlib import Path

from .store import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS link_manifest (
 manifest_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batch(id),
 digest TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS link_manual_plan (
 plan_id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL REFERENCES link_manifest(manifest_id),
 digest TEXT NOT NULL, raw BLOB NOT NULL, result TEXT NOT NULL);
"""


def record(db_path, input_path):
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw)
    left = doc['left_ids']
    right = doc['right_ids']
    if not doc['manifest_id'] or not left or len(left) != len(right):
        raise ValueError('link_bad_manifest')
    if len(left) != len(set(left)) or len(right) != len(set(right)):
        raise ValueError('link_duplicate_record')
    pair_keys = set()
    claim_ids = set()
    for edge in doc['edges']:
        pair = edge['left'], edge['right']
        if pair[0] not in left or pair[1] not in right or pair in pair_keys:
            raise ValueError('link_bad_edge')
        pair_keys.add(pair)
        if len(edge['claims']) < 2:
            raise ValueError('link_insufficient_claims')
        sources = set()
        for claim in edge['claims']:
            score = claim['score']
            if not claim['id'] or claim['id'] in claim_ids or not claim['source'] or claim['source'] in sources:
                raise ValueError('link_duplicate_claim')
            if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 10:
                raise ValueError('link_bad_score')
            claim_ids.add(claim['id'])
            sources.add(claim['source'])
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        batch = db.execute('SELECT config_raw FROM batch WHERE id=?',
                           (doc['batch_id'],)).fetchone()
        if not batch:
            raise ValueError('link_unknown_batch')
        dataset = json.loads(batch['config_raw'])['dataset']
        if dataset['version'] != doc['dataset_version']:
            raise ValueError('link_dataset_mismatch')
        prior = db.execute('SELECT digest FROM link_manifest WHERE manifest_id=?',
                           (doc['manifest_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('link_manifest_conflict')
            return doc['manifest_id']
        with db:
            db.execute('INSERT INTO link_manifest VALUES (?,?,?,?)',
                       (doc['manifest_id'], doc['batch_id'], digest, raw))
        return doc['manifest_id']
    finally:
        db.close()


def evaluate_manual(db_path, input_path):
    raw = Path(input_path).read_bytes()
    plan = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        prior = db.execute('SELECT digest,result FROM link_manual_plan WHERE plan_id=?',
                           (plan['plan_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('link_plan_conflict')
            return json.loads(prior['result'])
        row = db.execute('SELECT raw FROM link_manifest WHERE manifest_id=?',
                         (plan['manifest_id'],)).fetchone()
        if not row:
            raise ValueError('link_unknown_manifest')
        doc = json.loads(row['raw'])
        edges = {(edge['left'], edge['right']): edge for edge in doc['edges']}
        pairs = [(item['left'], item['right']) for item in plan['pairs']]
        if len(pairs) != len(doc['left_ids']) or {x for x, _ in pairs} != set(doc['left_ids']) or (
                {y for _, y in pairs} != set(doc['right_ids'])):
            raise ValueError('link_not_one_to_one_complete')
        if any(pair not in edges for pair in pairs):
            raise ValueError('link_unsupported_pair')
        chosen = []
        for left, right in pairs:
            edge = edges[left, right]
            score = min(claim['score'] for claim in edge['claims'])
            chosen.append({'left': left, 'right': right, 'score': score,
                           'claims': sorted(claim['id'] for claim in edge['claims'])})
        chosen.sort(key=lambda item: item['left'])
        result = {'manifest_id': plan['manifest_id'], 'plan_id': plan['plan_id'],
                  'pairs': chosen, 'total_score': sum(item['score'] for item in chosen),
                  'minimum_score': min(item['score'] for item in chosen)}
        with db:
            db.execute('INSERT INTO link_manual_plan VALUES (?,?,?,?,?)',
                       (plan['plan_id'], plan['manifest_id'], digest, raw,
                        json.dumps(result, sort_keys=True)))
        return result
    finally:
        db.close()
