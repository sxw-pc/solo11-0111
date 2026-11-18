"""Synthetic microtable intake, manual group evaluation and exact optimization.

Three workflows share one frozen source table:

* ``record`` freezes the raw synthetic microtable and its hierarchies in
  SQLite after validating the batch/dataset binding and every hierarchy.
* ``evaluate_manual`` evaluates a caller-supplied partition: common
  generalization ancestor per group, k-anonymity, l-diversity and weighted
  loss.  It never searches for a better partition and never publishes rows.
* ``optimize`` computes the provably minimum-loss compliant transformation
  (subset-DP set partition over bounded candidate classes), persists the
  internal row audit mapping and the actual anonymized row table, and can be
  independently recomputed by ``verify_optimization``.
"""

import hashlib
import json
from pathlib import Path

from .anon_optimize import (
    HierarchyError,
    build_publication,
    common_ancestor,
    evaluate_class,
    sensitive_token,
    solve,
    validate_document,
)
from .store import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS anon_source (
 source_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batch(id),
 digest TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS anon_manual_plan (
 plan_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES anon_source(source_id),
 digest TEXT NOT NULL, raw BLOB NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS anon_opt_plan (
 plan_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES anon_source(source_id),
 request_digest TEXT NOT NULL, source_digest TEXT NOT NULL,
 raw BLOB NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS anon_opt_row (
 plan_id TEXT NOT NULL REFERENCES anon_opt_plan(plan_id),
 row_id TEXT NOT NULL, eq_index INTEGER NOT NULL,
 published_qi TEXT NOT NULL, loss INTEGER NOT NULL,
 PRIMARY KEY(plan_id,row_id));
"""


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def record(db_path, input_path):
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw)
    # Raises with explicit codes for empty sources, duplicate row ids and any
    # non-tree/cyclic/cost-inverting hierarchy.
    validate_document(doc)
    # A missing or null sensitive value is retained but never counts toward
    # l-diversity; the column name itself is still mandatory.
    if not doc.get('sensitive_field'):
        raise ValueError('anon_bad_hierarchy')
    digest = _digest(raw)
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
    node = common_ancestor(values, spec)
    if node is None:
        raise ValueError('anon_no_common_generalization')
    return node


def evaluate_manual(db_path, input_path):
    raw = Path(input_path).read_bytes()
    plan = json.loads(raw)
    digest = _digest(raw)
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
            distinct = len({sensitive_token(rows[row_id], doc['sensitive_field'])
                            for row_id in group} - {None})
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


def _load_source(db, source_id):
    row = db.execute('SELECT raw,digest,batch_id FROM anon_source WHERE source_id=?',
                     (source_id,)).fetchone()
    if not row:
        raise ValueError('anon_unknown_source')
    doc = json.loads(row['raw'])
    if _digest(row['raw']) != row['digest']:
        raise ValueError('anon_source_corrupt')
    return doc, row['digest'], row['batch_id']


def _canonical_request(request):
    return json.dumps(request, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def optimize(db_path, input_path):
    """Compute and persist the exact minimum-loss anonymized transformation."""
    raw = Path(input_path).read_bytes()
    request = json.loads(raw)
    for key in ('plan_id', 'source_id', 'k', 'l_distinct'):
        if key not in request:
            raise ValueError(f'anon_missing_request:{key}')
    k, diversity = request['k'], request['l_distinct']
    if any(isinstance(x, bool) or not isinstance(x, int) or x < 1 for x in (k, diversity)):
        raise ValueError('anon_bad_threshold')
    request_digest = _digest(_canonical_request(request))
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        prior = db.execute('SELECT raw,result FROM anon_opt_plan WHERE plan_id=?',
                           (request['plan_id'],)).fetchone()
        if prior:
            if _digest(_canonical_request(json.loads(prior['raw']))) != request_digest:
                raise ValueError('anon_plan_conflict')
            return json.loads(prior['result'])
        doc, source_digest, batch_id = _load_source(db, request['source_id'])
        solution = solve(doc, k, diversity,
                         candidate_budget=request.get('candidate_budget'),
                         dp_budget=request.get('dp_budget'))
        result = {
            'plan_id': request['plan_id'],
            'source_id': request['source_id'],
            'source_digest': source_digest,
            'request_digest': request_digest,
            'batch_id': batch_id,
            'dataset_version': doc['dataset_version'],
            'contract': {'k': k, 'l_distinct': diversity},
            'status': solution['status'],
            'loss': solution['loss'] if solution['status'] == 'optimal' else None,
            'lower_bound': solution['lower_bound'],
            'upper_bound': solution['upper_bound'],
            'budget': solution['budget'],
            'quasi_identifiers': doc['quasi_identifiers'],
            'sensitive_field': doc['sensitive_field'],
        }
        partition_ids = None
        if solution['partition'] is not None:
            partition_ids = [[doc['rows'][i]['id'] for i in members]
                             for members in solution['partition']]
            publication = build_publication(doc, solution['partition'])
            result['internal_groups'] = partition_ids
            result['equivalence_classes'] = publication['equivalence_classes']
            result['row_audit'] = publication['row_audit']
            result['published_rows'] = publication['published_rows']
            result['loss_breakdown'] = [
                {'eq_index': eq['eq_index'], 'loss': eq['loss'],
                 'column_loss': eq['column_loss']}
                for eq in publication['equivalence_classes']]
        with db:
            db.execute('INSERT INTO anon_opt_plan VALUES (?,?,?,?,?,?)',
                       (request['plan_id'], request['source_id'], request_digest,
                        source_digest, _canonical_request(request),
                        json.dumps(result, sort_keys=True, ensure_ascii=False)))
            if partition_ids is not None:
                per_class = {eq['eq_index']: eq for eq in result['equivalence_classes']}
                for audit in result['row_audit']:
                    eq = per_class[audit['eq_index']]
                    db.execute('INSERT INTO anon_opt_row VALUES (?,?,?,?,?)',
                               (request['plan_id'], audit['row_id'], audit['eq_index'],
                                json.dumps(audit['published_qi'], sort_keys=True),
                                eq['per_row_loss']))
        return result
    finally:
        db.close()


def verify_optimization(db_path, plan_id):
    """Independently recompute everything from the frozen source and contract.

    Nothing in the persisted result is trusted: hierarchies, the optimum, the
    published nodes, row coverage, class sizes, diversity and losses are all
    re-derived from the frozen source bytes.  Returns a dict with an empty
    ``problems`` list only when the stored artifact fully matches the
    independent computation.
    """
    db = connect(db_path)
    problems = []
    try:
        db.executescript(SCHEMA)
        stored = db.execute('SELECT * FROM anon_opt_plan WHERE plan_id=?',
                            (plan_id,)).fetchone()
        if not stored:
            return {'plan_id': plan_id, 'ok': False,
                    'problems': ['anon_verify_unknown_plan']}
        result = json.loads(stored['result'])
        request = json.loads(stored['raw'])
        if _digest(_canonical_request(request)) != stored['request_digest']:
            problems.append('anon_verify_request_digest')
        try:
            doc, source_digest, _ = _load_source(db, stored['source_id'])
        except ValueError:
            # The frozen source bytes themselves can no longer be trusted, so
            # no recomputation is possible: report rather than reuse stored data.
            return {'plan_id': plan_id, 'ok': False,
                    'problems': sorted(set(problems + ['anon_verify_source_digest']))}
        if source_digest != stored['source_digest']:
            problems.append('anon_verify_source_digest')
        k, diversity = request['k'], request['l_distinct']

        fresh = solve(doc, k, diversity,
                      candidate_budget=request.get('candidate_budget'),
                      dp_budget=request.get('dp_budget'))
        if fresh['status'] != result['status']:
            problems.append('anon_verify_status_mismatch')
        if fresh['lower_bound'] != result['lower_bound']:
            problems.append('anon_verify_lower_bound')
        if fresh['upper_bound'] != result['upper_bound']:
            problems.append('anon_verify_upper_bound')
        if fresh['status'] == 'optimal':
            if result.get('loss') != fresh['loss']:
                problems.append('anon_verify_suboptimal')
            if result.get('internal_groups') != partition_to_ids(doc, fresh['partition']):
                problems.append('anon_verify_partition_mismatch')

        if fresh['partition'] is not None:
            problems.extend(_check_publication(doc, result, k, diversity,
                                               fresh, source_digest))
        elif result.get('published_rows'):
            problems.append('anon_verify_publication_without_partition')
        problems.extend(_check_row_table(db, plan_id, result))
        return {'plan_id': plan_id, 'ok': not problems, 'problems': sorted(set(problems)),
                'recomputed_status': fresh['status'],
                'recomputed_loss': fresh['loss'] if fresh['status'] == 'optimal' else None,
                'recomputed_bounds': {'lower': fresh['lower_bound'],
                                      'upper': fresh['upper_bound']}}
    finally:
        db.close()


def _check_row_table(db, plan_id, result):
    """The SQL audit table must agree with the JSON artifact, row for row."""
    problems = []
    expected = result.get('row_audit')
    if not isinstance(expected, list):
        return problems
    stored_rows = db.execute(
        'SELECT row_id,eq_index,published_qi,loss FROM anon_opt_row WHERE plan_id=?',
        (plan_id,)).fetchall()
    if len(stored_rows) != len(expected):
        problems.append('anon_verify_row_table_mismatch')
        return problems
    per_class_loss = {eq['eq_index']: eq['per_row_loss']
                      for eq in result.get('equivalence_classes', [])}
    by_id = {row['row_id']: row for row in stored_rows}
    for item in expected:
        row = by_id.get(item['row_id'])
        if row is None or row['eq_index'] != item['eq_index']:
            problems.append('anon_verify_row_table_mismatch')
            continue
        if json.loads(row['published_qi']) != item['published_qi']:
            problems.append('anon_verify_row_table_mismatch')
        if row['loss'] != per_class_loss.get(item['eq_index']):
            problems.append('anon_verify_row_table_mismatch')
    return problems


def partition_to_ids(doc, partition):
    return [[doc['rows'][i]['id'] for i in members] for members in partition]


def _check_publication(doc, result, k, diversity, fresh, source_digest):
    problems = []
    qi_fields = doc['quasi_identifiers']
    rows = doc['rows']
    row_ids = [row['id'] for row in rows]
    by_id = {row['id']: row for row in rows}

    audit = result.get('row_audit')
    published = result.get('published_rows')
    classes = result.get('equivalence_classes')
    if not isinstance(audit, list) or not isinstance(published, list) or not isinstance(classes, list):
        return ['anon_verify_publication_structure']

    # 1. Exact row coverage: every frozen row once, none dropped or duplicated.
    if any(not isinstance(item, dict) for item in audit) or any(
            not isinstance(row, dict) for row in published):
        return ['anon_verify_publication_structure']
    audited = [item.get('row_id') for item in audit]
    if sorted(audited) != sorted(row_ids) or len(audited) != len(set(audited)):
        problems.append('anon_verify_row_set_mismatch')

    # 2. The published row table must not leak an internal row id.
    if any('row_id' in row for row in published):
        problems.append('anon_verify_internal_id_leak')
    if len(published) != len(rows):
        problems.append('anon_verify_row_set_mismatch')

    recomputed = build_publication(doc, fresh['partition'])
    if _canonical_request(result.get('equivalence_classes', [])) != _canonical_request(
            recomputed['equivalence_classes']):
        problems.append('anon_verify_classes_mismatch')
    if _canonical_request(result.get('published_rows', [])) != _canonical_request(
            recomputed['published_rows']):
        problems.append('anon_verify_published_rows_mismatch')
    if _canonical_request(result.get('row_audit', [])) != _canonical_request(
            recomputed['row_audit']):
        problems.append('anon_verify_row_audit_mismatch')

    # 3. Defensive, source-derived checks that do not rely on the solver output.
    for item in audit:
        row = by_id.get(item.get('row_id'))
        if row is None:
            continue
        for field in qi_fields:
            node = item.get('published_qi', {}).get(field)
            chain = doc['hierarchies'][field]['ancestors'].get(str(row[field]))
            if chain is None or node not in chain:
                problems.append('anon_verify_forged_node')

    total = 0
    for eq in classes:
        if not isinstance(eq, dict) or 'qi' not in eq:
            problems.append('anon_verify_publication_structure')
            continue
        members = eq.get('row_ids', [])
        if len(members) != eq.get('size') or len(members) < k:
            problems.append('anon_verify_class_size')
        tokens = {sensitive_token(by_id[rid], doc['sensitive_field'])
                  for rid in members if rid in by_id}
        tokens.discard(None)
        if len(tokens) != eq.get('sensitive_distinct') or len(tokens) < diversity:
            problems.append('anon_verify_diversity')
        candidate = evaluate_class(tuple(sorted(
            row_ids.index(rid) for rid in members if rid in by_id)), doc)
        if candidate is None:
            problems.append('anon_verify_forged_node')
        else:
            if tuple(candidate['nodes'][f] for f in qi_fields) != tuple(
                    eq['qi'][f] for f in qi_fields):
                problems.append('anon_verify_forged_node')
            total += candidate['cost']
            if candidate['cost'] != eq.get('loss'):
                problems.append('anon_verify_loss_mismatch')
            for field in qi_fields:
                expected_col = len(members) * doc['hierarchies'][field]['costs'][
                    candidate['nodes'][field]]
                if eq.get('column_loss', {}).get(field) != expected_col:
                    problems.append('anon_verify_loss_mismatch')
        # Every member must actually be assigned to this published tuple.
        for item in audit:
            if item.get('row_id') in members and item.get('eq_index') != eq['eq_index']:
                problems.append('anon_verify_merge_mismatch')

    # 4. Equal published tuples must be one equivalence class, not two ids.
    seen_tuples = {}
    for eq in classes:
        key = tuple(eq['qi'][f] for f in qi_fields)
        if key in seen_tuples:
            problems.append('anon_verify_merge_mismatch')
        seen_tuples[key] = eq['eq_index']

    if result.get('loss') is not None:
        if total != result['loss']:
            problems.append('anon_verify_loss_mismatch')
        if result['status'] == 'optimal' and total != fresh['loss']:
            problems.append('anon_verify_suboptimal')
    if result.get('source_digest') != source_digest:
        problems.append('anon_verify_source_digest')
    if result.get('contract') != {'k': k, 'l_distinct': diversity}:
        problems.append('anon_verify_contract_mismatch')
    return problems
