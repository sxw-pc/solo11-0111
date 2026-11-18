"""Exact minimum-loss k-anonymity / l-diversity optimizer over local synthetic
microtables.

The optimizer is intentionally a pure, side-effect-free module so that
``anon-verify`` can recompute every published value from the frozen source
without trusting any persisted result.

Model
-----
* Each quasi-identifier column carries a rooted tree hierarchy given as the
  full ancestor chain of every leaf value.  Local recoding is allowed: the
  same column may be generalized at different nodes in different equivalence
  classes, but within one class every row shares one published node.
* A candidate class is a subset of source rows whose size is at least ``k`` and
  whose number of distinct non-missing sensitive values is at least
  ``l_distinct``.  The published node per column is the cheapest common
  ancestor of every original value in the class (ties broken by node label).
* Information loss is additive: for every class, ``size * sum(column costs)``.
* The problem is an exact set-partition problem solved with subset dynamic
  programming over row bitmasks, plus an enumeration budget.  Nearest-neighbour
  pairing and global-only generalization are deliberately never used.  When a
  resource budget is exhausted the result is reported as ``indeterminate``
  together with proven lower/upper bounds instead of being mislabelled optimal.
* Two internal classes that publish the identical quasi-identifier tuple are
  always merged before k/l re-checking: published equivalence classes are keyed
  by their actual tuple, never by an internal group id.
"""

from itertools import combinations

# Hard caps keep the exact search finite even when a caller passes absurd
# budgets.  They comfortably cover the required >=12-row / 2-3 column cases.
MAX_ENUMERATED = 2_000_000
MAX_DP_OPS = 8_000_000
DEFAULT_ENUMERATED = 60_000
DEFAULT_DP_OPS = 2_000_000


class HierarchyError(ValueError):
    """Raised when a quasi-identifier hierarchy is not a valid lossy tree."""


def validate_hierarchy(spec):
    """Validate one column hierarchy.

    Ancestor chains must start at the leaf itself, be node-disjoint except for
    shared ancestors, agree on the ordering of shared nodes (a single tree),
    contain no cycles, and every referenced node needs a non-negative cost.
    Costs must not invert along an ancestor edge, otherwise cheaper published
    data would disclose *more* than the originals and the loss model would be
    meaningless.
    """
    if not isinstance(spec, dict) or 'ancestors' not in spec or 'costs' not in spec:
        raise HierarchyError('anon_bad_hierarchy')
    ancestors, costs = spec['ancestors'], spec['costs']
    if not isinstance(ancestors, dict) or not ancestors:
        raise HierarchyError('anon_bad_hierarchy')
    if not isinstance(costs, dict):
        raise HierarchyError('anon_bad_hierarchy')
    for node, cost in costs.items():
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
            raise HierarchyError('anon_bad_cost')
    position = {}
    for leaf, chain in ancestors.items():
        if not isinstance(chain, list) or not chain or chain[0] != leaf or len(chain) != len(set(chain)):
            raise HierarchyError('anon_bad_hierarchy')
        seen = set()
        for depth, node in enumerate(chain):
            if not isinstance(node, str):
                raise HierarchyError('anon_bad_hierarchy')
            if node not in costs:
                raise HierarchyError('anon_bad_hierarchy')
            seen.add(node)
            prior = position.get(node)
            if prior is not None and prior != depth:
                # Same node reached at two different depths => non-tree DAG.
                raise HierarchyError('anon_non_tree_hierarchy')
            position[node] = depth
        for depth in range(1, len(chain)):
            child, parent = chain[depth - 1], chain[depth]
            if costs[parent] < costs[child]:
                raise HierarchyError('anon_cost_inversion')
    dangling = set(costs) - set(position)
    if dangling:
        raise HierarchyError('anon_bad_hierarchy')
    return True


def validate_document(doc):
    """Validate the structure shared by intake, optimization and verification."""
    if not doc.get('source_id') or not doc.get('rows') or not doc.get('quasi_identifiers'):
        raise ValueError('anon_empty_source')
    rows = doc['rows']
    row_ids = [row['id'] for row in rows]
    if len(row_ids) != len(set(row_ids)) or not all(row_ids):
        raise ValueError('anon_duplicate_row')
    hierarchies = doc.get('hierarchies')
    if not isinstance(hierarchies, dict):
        raise ValueError('anon_bad_hierarchy')
    qi_fields = doc['quasi_identifiers']
    if len(set(qi_fields)) != len(qi_fields) or not qi_fields:
        raise HierarchyError('anon_bad_hierarchy')
    for field in qi_fields:
        spec = hierarchies.get(field)
        validate_hierarchy(spec)
        for row in rows:
            if field not in row:
                raise ValueError('anon_missing_hierarchy_value')
            value = str(row[field])
            chain = spec['ancestors'].get(value)
            if not chain or chain[0] != value or any(node not in spec['costs'] for node in chain):
                raise ValueError('anon_missing_hierarchy_value')
    sensitive = doc['sensitive_field']
    if not sensitive:
        raise ValueError('anon_bad_hierarchy')
    return True


def sensitive_token(row, sensitive_field):
    """Missing/None sensitive values do not contribute to l-diversity."""
    value = row.get(sensitive_field)
    if value is None:
        return None
    return str(value)


def common_ancestor(values, spec):
    """Cheapest common ancestor node; stable tie-break on the node label."""
    common = set(spec['ancestors'][str(values[0])])
    for value in values[1:]:
        common &= set(spec['ancestors'][str(value)])
    if not common:
        return None
    return min(common, key=lambda node: (spec['costs'][node], node))


def evaluate_class(members, doc):
    """Return the exact published tuple, per-row loss and diversity for a class.

    ``members`` is a tuple of row indices into ``doc['rows']`` in frozen order.
    Returns ``None`` when some column has no common ancestor for these rows.
    """
    rows = doc['rows']
    qi_fields = doc['quasi_identifiers']
    nodes, per_row_loss = {}, 0
    for field in qi_fields:
        spec = doc['hierarchies'][field]
        node = common_ancestor([rows[i][field] for i in members], spec)
        if node is None:
            return None
        nodes[field] = node
        per_row_loss += spec['costs'][node]
    sensitive = doc['sensitive_field']
    tokens = {sensitive_token(rows[i], sensitive) for i in members}
    tokens.discard(None)
    return {
        'members': tuple(members),
        'nodes': nodes,
        'per_row_loss': per_row_loss,
        'cost': len(members) * per_row_loss,
        'sensitive_distinct': len(tokens),
        'sensitive_values': sorted(tokens),
    }


def _enumerate_candidates(n, doc, k, l_distinct, candidate_budget):
    """Enumerate every feasible candidate class, anchored on its lowest row.

    Anchoring guarantees each subset is generated exactly once and lets the
    subset DP only consider classes covering the first uncovered row.
    """
    anchored = [[] for _ in range(n)]
    enumerated = 0
    truncated = False
    for anchor in range(n):
        rest = [i for i in range(anchor + 1, n)]
        for width in range(k - 1, len(rest) + 1):
            for extra in combinations(rest, width):
                enumerated += 1
                if enumerated > candidate_budget:
                    truncated = True
                    return anchored, truncated, enumerated
                members = (anchor,) + extra
                candidate = evaluate_class(members, doc)
                if candidate is None:
                    continue
                if candidate['sensitive_distinct'] < l_distinct:
                    continue
                bitmask = sum(1 << i for i in members)
                candidate['mask'] = bitmask
                candidate['sig'] = tuple(sorted(members))
                anchored[anchor].append(candidate)
    return anchored, truncated, enumerated


def _greedy_upper_bound(n, anchored):
    """A feasible (not necessarily optimal) partition giving a proven UB."""
    remaining = (1 << n) - 1
    partition, total = [], 0
    while remaining:
        anchor = (remaining & -remaining).bit_length() - 1
        usable = [c for c in anchored[anchor] if c['mask'] & remaining == c['mask']]
        if not usable:
            return None
        best = min(usable, key=lambda c: (c['per_row_loss'], c['sig']))
        partition.append(best)
        total += best['cost']
        remaining ^= best['mask']
    return total, tuple(sorted((c['sig'] for c in partition), key=lambda sig: sig[0]))


def proven_lower_bound(doc):
    """Per-row cheapest reachable node, summed over all rows.

    Every row must be published at one of its chain nodes in every feasible
    transformation, so the cheapest node cost per column is a row-wise lower
    bound.  The sum is therefore a valid global lower bound even when candidate
    enumeration was cut short.
    """
    total = 0.0
    for row in doc['rows']:
        for field in doc['quasi_identifiers']:
            spec = doc['hierarchies'][field]
            chain = spec['ancestors'][str(row[field])]
            total += min(spec['costs'][node] for node in chain)
    return total


def solve(doc, k, l_distinct, candidate_budget=DEFAULT_ENUMERATED,
          dp_budget=DEFAULT_DP_OPS):
    """Exact subset-DP set partition with bounded fallback.

    Returns a dict containing ``status`` (``optimal`` / ``infeasible`` /
    ``indeterminate``), the canonical partition or bounds, and budget usage.
    """
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError('anon_bad_threshold')
    if isinstance(l_distinct, bool) or not isinstance(l_distinct, int) or l_distinct < 1:
        raise ValueError('anon_bad_threshold')
    validate_document(doc)
    n = len(doc['rows'])
    if candidate_budget is None:
        candidate_budget = DEFAULT_ENUMERATED
    if dp_budget is None:
        dp_budget = DEFAULT_DP_OPS
    candidate_budget = min(max(1, int(candidate_budget)), MAX_ENUMERATED)
    dp_budget = min(max(1, int(dp_budget)), MAX_DP_OPS)

    anchored, truncated, enumerated = _enumerate_candidates(
        n, doc, k, l_distinct, candidate_budget)
    lb = proven_lower_bound(doc)

    full = (1 << n) - 1
    # dp[mask] = (minimum loss, canonical partition signature) for exactly mask.
    dp = {0: (0, ())}
    ops = 0
    dp_truncated = False
    for mask in range(full + 1):
        if mask not in dp:
            continue
        base_cost, base_sig = dp[mask]
        anchor = ((full ^ mask) & -(full ^ mask)).bit_length() - 1
        for cand in anchored[anchor]:
            ops += 1
            if ops > dp_budget:
                dp_truncated = True
                break
            if cand['mask'] & mask:
                continue
            new_mask = mask | cand['mask']
            new_sig = tuple(sorted(base_sig + (cand['sig'],)))
            new_cost = base_cost + cand['cost']
            old = dp.get(new_mask)
            if old is None or (new_cost, new_sig) < old:
                dp[new_mask] = (new_cost, new_sig)
        if dp_truncated:
            break

    greedy = _greedy_upper_bound(n, anchored)
    budget_used = {'enumerated': enumerated, 'dp_ops': ops,
                   'candidate_budget': candidate_budget, 'dp_budget': dp_budget,
                   'enumeration_truncated': truncated, 'dp_truncated': dp_truncated}

    if not truncated and not dp_truncated and full in dp:
        cost, sig = dp[full]
        return {'status': 'optimal', 'n': n, 'k': k, 'l_distinct': l_distinct,
                'loss': cost, 'partition': sig, 'lower_bound': cost,
                'upper_bound': cost, 'budget': budget_used}
    if not truncated and not dp_truncated:
        # Enumeration and DP both finished without covering all rows.
        return {'status': 'infeasible', 'n': n, 'k': k, 'l_distinct': l_distinct,
                'lower_bound': lb, 'upper_bound': None, 'partition': None,
                'budget': budget_used}

    ub = greedy[0] if greedy else None
    partition = greedy[1] if greedy else None
    return {'status': 'indeterminate', 'n': n, 'k': k, 'l_distinct': l_distinct,
            'lower_bound': lb, 'upper_bound': ub, 'partition': partition,
            'budget': budget_used}


def build_publication(doc, partition):
    """Turn a canonical partition into merged equivalence classes and rows.

    Internal groups that publish the same quasi-identifier tuple are merged
    into one actual equivalence class before size/diversity re-checking.  The
    published row table never carries an internal row id; the audit mapping
    keeps ``row_id -> equivalence class`` separately.
    """
    rows = doc['rows']
    qi_fields = doc['quasi_identifiers']
    internal = [evaluate_class(tuple(members), doc) for members in partition]
    if any(candidate is None for candidate in internal):
        raise ValueError('anon_no_common_generalization')

    order, by_tuple = [], {}
    for candidate in internal:
        key = tuple(candidate['nodes'][field] for field in qi_fields)
        if key not in by_tuple:
            by_tuple[key] = {'tuple': key, 'members': []}
            order.append(key)
        by_tuple[key]['members'].extend(candidate['members'])

    classes, row_audit, published_rows = [], [], []
    total_loss = 0
    for eq_index, key in enumerate(order):
        merged_members = tuple(sorted(by_tuple[key]['members']))
        merged = evaluate_class(merged_members, doc)
        total_loss += merged['cost']
        classes.append({
            'eq_index': eq_index,
            'qi': {field: key[pos] for pos, field in enumerate(qi_fields)},
            'row_ids': [rows[i]['id'] for i in merged_members],
            'size': len(merged_members),
            'sensitive_distinct': merged['sensitive_distinct'],
            'sensitive_values': merged['sensitive_values'],
            'per_row_loss': merged['per_row_loss'],
            'loss': merged['cost'],
            'column_loss': {field: len(merged_members) * doc['hierarchies'][field]['costs'][key[pos]]
                            for pos, field in enumerate(qi_fields)},
        })
        for i in merged_members:
            row_audit.append({'row_id': rows[i]['id'], 'eq_index': eq_index,
                              'original_qi': {field: str(rows[i][field]) for field in qi_fields},
                              'published_qi': {field: key[pos] for pos, field in enumerate(qi_fields)}})
            published_rows.append({
                'eq_index': eq_index,
                **{field: key[pos] for pos, field in enumerate(qi_fields)},
                doc['sensitive_field']: sensitive_token(rows[i], doc['sensitive_field']),
            })

    row_positions = {row['id']: i for i, row in enumerate(rows)}
    row_audit.sort(key=lambda item: row_positions[item['row_id']])
    published_rows.sort(key=lambda item: (item['eq_index'],))
    return {'equivalence_classes': classes, 'row_audit': row_audit,
            'published_rows': published_rows, 'loss': total_loss}
