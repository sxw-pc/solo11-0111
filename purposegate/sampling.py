"""Freeze a synthetic data-product frame, evaluate a manual allocation, and
compute provably optimal stratified allocations under a frozen contract.

sample-optimize chooses integer per-stratum sample counts n_h
(1 <= n_h <= N_h) with total cost <= budget. The contract (objective plus
optional step limit) is frozen by raw-byte SHA before optimization, and all
comparisons use exact rationals (Fraction); nothing is rounded before a
choice is made.

  - min_design_variance: minimize the pilot design variance of the
    population mean, sum_h (N_h/N)^2 * S_h^2 * (1 - n_h/N_h) / n_h. Ties
    break on the smaller coverage spread max(n_h/N_h) - min(n_h/N_h), then
    on the allocation tuple in stratum-id order (lexicographically smallest
    wins).
  - min_coverage_gap: minimize the coverage spread. Ties break on larger
    total sampled rows, then smaller pilot design variance, then the same
    lexicographic rule.

The variance objective is separable, so an exact budget DP certifies the
optimal variance and a tie-break search only visits allocations the DP
certifies; the coverage objective is resolved by an exact scan over
rate windows followed by a certified tie-break search. If the step budget
is exhausted the plan is reported indeterminate with proven bounds and no
pseudo-optimal sample is drawn.

Once an allocation is chosen, the deterministic priority order (SHA-256 of
seed + NUL + row id) draws the actual row ids. The reported n/N is the
ex-ante inclusion probability of the frozen design under a uniform priority
order model; the fixed seed only reproduces this one roster, and a frozen
roster must not be read as still random after the fact.

sample-verify independently recomputes the allocation, roster, inclusion
rates and metrics from the frozen frame bytes, the frozen contract and the
priority order, and flags forged objective values, reused row ids, misused
costs or a replaced seed. The manual allocation evaluation is preserved.
"""

import hashlib
import json
import math
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
CREATE TABLE IF NOT EXISTS sample_optimize_plan (
 plan_id TEXT PRIMARY KEY, frame_id TEXT NOT NULL REFERENCES sample_frame(frame_id),
 objective TEXT NOT NULL, digest TEXT NOT NULL, raw BLOB NOT NULL, result TEXT NOT NULL);
"""

OBJECTIVES = ('min_design_variance', 'min_coverage_gap')
DEFAULT_STEP_LIMIT = 1000000


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError('sample_bad_positive_integer')
    return value


def _priority_order(seed, members):
    return sorted(members, key=lambda member: (
        hashlib.sha256((seed + '\0' + member).encode()).hexdigest(), member))


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
            ordered = _priority_order(frame['priority_seed'], members)
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


class _StepLimit(Exception):
    pass


def _validate_contract(doc):
    if not isinstance(doc, dict):
        raise ValueError('sample_bad_contract')
    plan_id = doc.get('plan_id')
    frame_id = doc.get('frame_id')
    if not plan_id or not isinstance(plan_id, str) or not frame_id or not isinstance(frame_id, str):
        raise ValueError('sample_bad_contract')
    objective = doc.get('objective')
    if objective not in OBJECTIVES:
        raise ValueError('sample_unknown_objective')
    limit = doc.get('step_limit', DEFAULT_STEP_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError('sample_bad_contract')
    return plan_id, frame_id, objective, limit


def _frame_model(frame):
    """Validated frame view: strata in id order with exact rationals."""
    if not frame.get('frame_id') or not frame.get('strata') or not frame.get('rows'):
        raise ValueError('sample_empty_frame')
    budget = _positive(frame['budget'])
    seed = frame.get('priority_seed')
    if not isinstance(seed, str) or not seed:
        raise ValueError('sample_bad_priority_seed')
    strata = {}
    for spec in frame['strata']:
        if not spec['id'] or spec['id'] in strata:
            raise ValueError('sample_duplicate_stratum')
        _positive(spec['cost_per_row'])
        _positive(spec['pilot_variance_num'])
        _positive(spec['pilot_variance_den'])
        strata[spec['id']] = spec
    seen = set()
    members = {key: [] for key in strata}
    for row in frame['rows']:
        if not row['id'] or row['id'] in seen or row['stratum'] not in strata:
            raise ValueError('sample_bad_row')
        seen.add(row['id'])
        members[row['stratum']].append(row['id'])
    if any(not ids for ids in members.values()):
        raise ValueError('sample_empty_stratum')
    ids = sorted(strata)
    return {'ids': ids, 'members': members, 'seed': seed, 'budget': budget,
            'counts': {sid: len(members[sid]) for sid in ids},
            'costs': {sid: strata[sid]['cost_per_row'] for sid in ids},
            'pilots': {sid: Fraction(strata[sid]['pilot_variance_num'],
                                     strata[sid]['pilot_variance_den']) for sid in ids},
            'total': len(frame['rows'])}


def _design_terms(model):
    """terms[h][n-1]: pilot design variance contributed by stratum h at n_h = n."""
    total = model['total']
    terms = []
    for sid in model['ids']:
        population = model['counts'][sid]
        weight = Fraction(population, total)
        pilot = model['pilots'][sid]
        terms.append([weight * weight * pilot * Fraction(population - n, population * n)
                      for n in range(1, population + 1)])
    return terms


def _variance_dp(terms, costs, budget):
    """dp[h][b]: exact minimum variance of the first h strata at exact cost b."""
    size = len(terms)
    dp = [[None] * (budget + 1) for _ in range(size + 1)]
    dp[0][0] = Fraction(0)
    for h in range(size):
        src, dst, cost = dp[h], dp[h + 1], costs[h]
        for b in range(budget + 1):
            base = src[b]
            if base is None:
                continue
            for i, term in enumerate(terms[h]):
                nb = b + (i + 1) * cost
                if nb > budget:
                    break
                value = base + term
                if dst[nb] is None or value < dst[nb]:
                    dst[nb] = value
    return dp


def _prefix_min(row):
    out = []
    running = None
    for value in row:
        if value is not None and (running is None or value < running):
            running = value
        out.append(running)
    return out


def _best_min_variance(terms, costs, counts, budget, step_limit):
    """Exact min design variance, then min coverage gap, then lex-smallest tuple."""
    size = len(terms)
    fwd = _variance_dp(terms, costs, budget)
    bwd = _variance_dp(terms[::-1], costs[::-1], budget)
    vstar = _prefix_min(fwd[size])[budget]
    # bwdmin[h][t]: exact min variance of strata h..size-1 within budget t.
    bwdmin = [_prefix_min(bwd[size - h]) for h in range(size + 1)]
    best = {'gap': None, 'tuple': None}
    nodes = [0]
    prefix = []

    def dfs(h, cost, var, lo, hi):
        nodes[0] += 1
        if nodes[0] > step_limit:
            raise _StepLimit()
        if h == size:
            if var != vstar:
                return
            gap = hi - lo
            candidate = tuple(prefix)
            if (best['gap'] is None or gap < best['gap']
                    or (gap == best['gap'] and candidate < best['tuple'])):
                best['gap'] = gap
                best['tuple'] = candidate
            return
        if best['gap'] is not None and lo is not None:
            if hi - lo > best['gap']:
                return
            if hi - lo == best['gap'] and tuple(prefix) > best['tuple'][:h]:
                return
        lower = bwdmin[h][budget - cost]
        if lower is None or var + lower > vstar:
            return
        for n in range(1, counts[h] + 1):
            added = n * costs[h]
            if cost + added > budget:
                break
            rate = Fraction(n, counts[h])
            prefix.append(n)
            dfs(h + 1, cost + added, var + terms[h][n - 1],
                rate if lo is None or rate < lo else lo,
                rate if hi is None or rate > hi else hi)
            prefix.pop()

    try:
        dfs(0, 0, Fraction(0), None, None)
    except _StepLimit:
        return None, vstar, best, nodes[0]
    return best['tuple'], vstar, best, nodes[0]


def _window_bounds(lo, hi, counts):
    los = []
    his = []
    for population in counts:
        low = max(1, math.ceil(lo * population))
        high = min(population, math.floor(hi * population))
        if low > high:
            return None
        los.append(low)
        his.append(high)
    return los, his


def _best_min_coverage(terms, costs, counts, budget, step_limit):
    """Exact min coverage gap, then max total rows, then min variance, then lex."""
    size = len(terms)
    rates = sorted({Fraction(n, population)
                    for population in counts for n in range(1, population + 1)})
    gstar = None
    pairs = sorted((rates[j] - rates[i], i, j)
                   for i in range(len(rates)) for j in range(i, len(rates)))
    for gap, i, j in pairs:
        bounds = _window_bounds(rates[i], rates[j], counts)
        if bounds and sum(c * low for c, low in zip(costs, bounds[0])) <= budget:
            gstar = gap
            break
    rstar = 0
    for lo in rates:
        bounds = _window_bounds(lo, lo + gstar, counts)
        if not bounds:
            continue
        los, his = bounds
        base = sum(c * low for c, low in zip(costs, los))
        if base > budget:
            continue
        remaining = budget - base
        rows = sum(los)
        # Linear costs: cheapest stratum first maximizes the row count.
        for h in sorted(range(size), key=lambda k: (costs[k], k)):
            take = min(his[h] - los[h], remaining // costs[h])
            rows += take
            remaining -= take * costs[h]
        if rows > rstar:
            rstar = rows
    relaxed = sum(terms[h][min(counts[h], budget // costs[h]) - 1] for h in range(size))
    mincost_tail = [0] * (size + 1)
    for h in range(size - 1, -1, -1):
        mincost_tail[h] = mincost_tail[h + 1] + costs[h]
    best = {'var': None, 'tuple': None}
    nodes = [0]
    prefix = []

    def dfs(h, cost, rows, var, lo, hi):
        nodes[0] += 1
        if nodes[0] > step_limit:
            raise _StepLimit()
        if lo is not None and hi - lo > gstar:
            return
        if h == size:
            if rows != rstar:
                return
            candidate = tuple(prefix)
            if (best['var'] is None or var < best['var']
                    or (var == best['var'] and candidate < best['tuple'])):
                best['var'] = var
                best['tuple'] = candidate
            return
        remaining = budget - cost
        if mincost_tail[h] > remaining:
            return
        if rows + (size - h) > rstar:
            return
        max_add = 0
        lower = Fraction(0)
        for k in range(h, size):
            cap = min(counts[k], remaining // costs[k])
            max_add += cap
            lower += terms[k][cap - 1]
        if rows + max_add < rstar:
            return
        if best['var'] is not None:
            if var + lower > best['var']:
                return
            if var + lower == best['var'] and tuple(prefix) > best['tuple'][:h]:
                return
        for n in range(1, counts[h] + 1):
            added = n * costs[h]
            if added > remaining:
                break
            rate = Fraction(n, counts[h])
            prefix.append(n)
            dfs(h + 1, cost + added, rows + n, var + terms[h][n - 1],
                rate if lo is None or rate < lo else lo,
                rate if hi is None or rate > hi else hi)
            prefix.pop()

    try:
        dfs(0, 0, 0, Fraction(0), None, None)
    except _StepLimit:
        return None, gstar, rstar, best, nodes[0], relaxed
    return best['tuple'], gstar, rstar, best, nodes[0], relaxed


def _frac_bounds(lower, upper):
    return {'lower_num': lower.numerator, 'lower_den': lower.denominator,
            'upper_num': upper.numerator if upper is not None else None,
            'upper_den': upper.denominator if upper is not None else None}


def _optimize_allocation(model, objective, step_limit):
    """Return (allocation tuple or None, info); None means indeterminate."""
    terms = _design_terms(model)
    costs = [model['costs'][sid] for sid in model['ids']]
    counts = [model['counts'][sid] for sid in model['ids']]
    budget = model['budget']
    if sum(costs) > budget:
        raise ValueError('sample_budget_below_minimum')
    if objective == 'min_design_variance':
        allocation, vstar, best, nodes = _best_min_variance(
            terms, costs, counts, budget, step_limit)
        info = {'optimality': {
            'method': 'exact_budget_dp',
            'certified': 'variance_optimal_then_min_coverage_gap_then_lex',
            'tie_break_order': ['design_variance', 'coverage_gap',
                                'allocation_lex_by_stratum_id'],
            'steps': nodes, 'step_limit': step_limit}}
        if allocation is None:
            info['status'] = 'indeterminate'
            info['reason'] = 'sample_step_limit_exceeded'
            info['bounds'] = {'variance': _frac_bounds(vstar, vstar),
                              'coverage_gap': _frac_bounds(Fraction(0), best['gap'])}
            return None, info
        info['status'] = 'optimal'
        return allocation, info
    allocation, gstar, rstar, best, nodes, relaxed = _best_min_coverage(
        terms, costs, counts, budget, step_limit)
    info = {'optimality': {
        'method': 'exact_rate_window_scan',
        'certified': 'min_coverage_gap_then_max_rows_then_min_variance_then_lex',
        'tie_break_order': ['coverage_gap', 'total_rows_desc', 'design_variance',
                            'allocation_lex_by_stratum_id'],
        'steps': nodes, 'step_limit': step_limit}}
    if allocation is None:
        info['status'] = 'indeterminate'
        info['reason'] = 'sample_step_limit_exceeded'
        info['bounds'] = {'coverage_gap': _frac_bounds(gstar, gstar),
                          'total_rows': {'lower': rstar, 'upper': rstar},
                          'variance': _frac_bounds(relaxed, best['var'])}
        return None, info
    info['status'] = 'optimal'
    return allocation, info


def _compute_plan_result(frame, contract, frame_digest, plan_digest):
    """Pure recompute of a plan result from frame content and frozen contract."""
    plan_id, frame_id, objective, step_limit = _validate_contract(contract)
    model = _frame_model(frame)
    allocation, info = _optimize_allocation(model, objective, step_limit)
    result = {'plan_id': plan_id, 'frame_id': frame_id, 'objective': objective,
              'status': info['status'], 'budget': model['budget'],
              'provenance': {'batch_id': frame.get('batch_id'),
                             'dataset_version': frame.get('dataset_version'),
                             'value_field': frame.get('value_field'),
                             'priority_seed': model['seed'],
                             'frame_id': frame.get('frame_id'),
                             'frame_digest': frame_digest,
                             'plan_digest': plan_digest},
              'optimality': info['optimality']}
    if info['status'] != 'optimal':
        result['reason'] = info['reason']
        result['bounds'] = info['bounds']
        return result
    selected = {}
    spent = 0
    variance = Fraction(0)
    coverages = []
    for sid, n in zip(model['ids'], allocation):
        population = model['counts'][sid]
        cost = model['costs'][sid]
        pilot = model['pilots'][sid]
        ordered = _priority_order(model['seed'], model['members'][sid])
        inclusion = Fraction(n, population)
        spent += n * cost
        weight = Fraction(population, model['total'])
        variance += weight * weight * pilot * Fraction(population - n, population * n)
        coverages.append(inclusion)
        selected[sid] = {'ids': ordered[:n], 'n': n, 'N': population,
                         'cost_per_row': cost, 'spent': n * cost,
                         'pilot_variance_num': pilot.numerator,
                         'pilot_variance_den': pilot.denominator,
                         'inclusion_num': inclusion.numerator,
                         'inclusion_den': inclusion.denominator}
    gap = max(coverages) - min(coverages)
    result.update({'allocation': dict(zip(model['ids'], allocation)),
                   'spent': spent, 'total_rows': sum(allocation),
                   'variance_num': variance.numerator,
                   'variance_den': variance.denominator,
                   'coverage_gap_num': gap.numerator,
                   'coverage_gap_den': gap.denominator,
                   'selected': selected})
    return result


def optimize_plan(db_path, input_path):
    """Optimize one frozen contract against one frozen frame; replay is idempotent."""
    raw = Path(input_path).read_bytes()
    contract = json.loads(raw)
    plan_id, frame_id, objective, _ = _validate_contract(contract)
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        prior = db.execute('SELECT digest,result FROM sample_optimize_plan WHERE plan_id=?',
                           (plan_id,)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('sample_optimize_conflict')
            return json.loads(prior['result'])
        row = db.execute('SELECT raw FROM sample_frame WHERE frame_id=?',
                         (frame_id,)).fetchone()
        if not row:
            raise ValueError('sample_unknown_frame')
        frame = json.loads(row['raw'])
        frame_digest = hashlib.sha256(row['raw']).hexdigest()
        result = _compute_plan_result(frame, contract, frame_digest, digest)
        with db:
            db.execute('INSERT INTO sample_optimize_plan VALUES (?,?,?,?,?,?)',
                       (plan_id, frame_id, objective, digest, raw,
                        json.dumps(result, sort_keys=True)))
        return result
    finally:
        db.close()


def _compare_result(problems, stored, recomputed):
    if stored.get('status') != recomputed.get('status'):
        problems.append('sample_verify_status')
        return
    if stored.get('status') != 'optimal':
        if (stored.get('bounds') != recomputed.get('bounds')
                or stored.get('reason') != recomputed.get('reason')):
            problems.append('sample_verify_bounds')
        return
    if stored.get('allocation') != recomputed.get('allocation'):
        problems.append('sample_verify_allocation')
    if any(stored.get(key) != recomputed.get(key) for key in
           ('variance_num', 'variance_den', 'coverage_gap_num', 'coverage_gap_den')):
        problems.append('sample_verify_objective_value')
    stored_sel = stored.get('selected')
    if not isinstance(stored_sel, dict) or set(stored_sel) != set(recomputed['selected']):
        problems.append('sample_verify_roster')
    else:
        for sid, want in recomputed['selected'].items():
            got = stored_sel[sid]
            if got.get('ids') != want['ids']:
                problems.append('sample_verify_roster')
            if (got.get('inclusion_num'), got.get('inclusion_den')) != (
                    want['inclusion_num'], want['inclusion_den']):
                problems.append('sample_verify_inclusion')
    if any(stored.get(key) != recomputed.get(key) for key in ('spent', 'budget', 'total_rows')):
        problems.append('sample_verify_cost')
    if stored.get('provenance') != recomputed.get('provenance'):
        problems.append('sample_verify_provenance')
    if stored.get('optimality') != recomputed.get('optimality'):
        problems.append('sample_verify_optimality')


def _audit_stored(problems, stored, frame):
    """Internal consistency of the stored result against the frozen frame."""
    if stored.get('status') != 'optimal':
        return
    try:
        model = _frame_model(frame)
    except ValueError as exc:
        problems.append(f'sample_verify_frame_invalid:{exc}')
        return
    membership = {row['id']: row['stratum'] for row in frame['rows']}
    selected = stored.get('selected')
    allocation = stored.get('allocation')
    if not isinstance(selected, dict) or not isinstance(allocation, dict):
        problems.append('sample_verify_roster')
        return
    seen = set()
    total_cost = 0
    for sid, sel in selected.items():
        ids = sel.get('ids') if isinstance(sel, dict) else None
        if not isinstance(ids, list):
            problems.append('sample_verify_roster')
            continue
        for rid in ids:
            if rid in seen:
                problems.append('sample_verify_roster_reused_id')
            seen.add(rid)
            if membership.get(rid) != sid:
                problems.append('sample_verify_roster_unknown_id')
        n = sel.get('n')
        if len(ids) != n or allocation.get(sid) != n:
            problems.append('sample_verify_roster_count')
        population = model['counts'].get(sid)
        cost = model['costs'].get(sid)
        if population is None or cost is None:
            problems.append('sample_verify_roster_unknown_id')
            continue
        try:
            if Fraction(sel['inclusion_num'], sel['inclusion_den']) != Fraction(n, population):
                problems.append('sample_verify_inclusion')
        except (KeyError, TypeError, ZeroDivisionError):
            problems.append('sample_verify_inclusion')
        if sel.get('spent') != n * cost or sel.get('cost_per_row') != cost:
            problems.append('sample_verify_cost')
        total_cost += n * cost
    if stored.get('spent') != total_cost or stored.get('spent', 0) > stored.get('budget', 0):
        problems.append('sample_verify_cost')
    try:
        terms = _design_terms(model)
        costs = [model['costs'][sid] for sid in model['ids']]
        counts = [model['counts'][sid] for sid in model['ids']]
        ns = [allocation[sid] for sid in model['ids']]
        if any(not isinstance(n, int) or n < 1 or n > N for n, N in zip(ns, counts)):
            raise ValueError('bad_allocation')
        if sum(n * c for n, c in zip(ns, costs)) > model['budget']:
            problems.append('sample_verify_cost')
        variance = sum(terms[h][ns[h] - 1] for h in range(len(ns)))
        rates = [Fraction(ns[h], counts[h]) for h in range(len(ns))]
        gap = max(rates) - min(rates)
        if (Fraction(stored['variance_num'], stored['variance_den']) != variance
                or Fraction(stored['coverage_gap_num'], stored['coverage_gap_den']) != gap):
            problems.append('sample_verify_objective_value')
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        problems.append('sample_verify_objective_value')


def verify_plan(db_path, plan_id):
    """Independently recompute a stored plan from the frozen frame and contract."""
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        row = db.execute('SELECT * FROM sample_optimize_plan WHERE plan_id=?',
                         (plan_id,)).fetchone()
        if not row:
            raise ValueError('sample_unknown_plan')
        problems = []
        plan_raw = row['raw']
        plan_digest = hashlib.sha256(plan_raw).hexdigest()
        if plan_digest != row['digest']:
            problems.append('sample_verify_plan_digest')
        try:
            contract = json.loads(plan_raw)
        except ValueError:
            contract = None
            problems.append('sample_verify_plan_parse')
        frow = db.execute('SELECT raw,digest FROM sample_frame WHERE frame_id=?',
                          (row['frame_id'],)).fetchone()
        frame = None
        recomputed = None
        if not frow:
            problems.append('sample_verify_unknown_frame')
        else:
            frame_digest = hashlib.sha256(frow['raw']).hexdigest()
            if frame_digest != frow['digest']:
                problems.append('sample_verify_frame_digest')
            try:
                frame = json.loads(frow['raw'])
            except ValueError:
                frame = None
                problems.append('sample_verify_frame_parse')
            if frame is not None and contract is not None:
                try:
                    recomputed = _compute_plan_result(frame, contract, frame_digest, plan_digest)
                except ValueError as exc:
                    problems.append(f'sample_verify_recompute:{exc}')
        try:
            stored = json.loads(row['result'])
        except ValueError:
            stored = None
            problems.append('sample_verify_result_parse')
        if isinstance(stored, dict):
            if recomputed is not None:
                _compare_result(problems, stored, recomputed)
            if frame is not None:
                _audit_stored(problems, stored, frame)
        unique = list(dict.fromkeys(problems))
        return {'plan_id': plan_id, 'frame_id': row['frame_id'],
                'ok': not unique, 'problems': unique, 'recomputed': recomputed}
    finally:
        db.close()


def format_plan_markdown(result):
    lines = [f"# 分层抽样计划 {result['plan_id']}", '']
    provenance = result['provenance']
    lines.append(f"- 抽样框：{result['frame_id']}（dataset {provenance['dataset_version']}，"
                 f"字段 {provenance['value_field']}，批次 {provenance['batch_id']}）")
    lines.append(f"- 合同目标：{result['objective']}；状态：{result['status']}")
    if result['status'] != 'optimal':
        lines.append(f"- 原因：{result.get('reason')}；已证明界限："
                     f"{json.dumps(result.get('bounds'), ensure_ascii=False, sort_keys=True)}")
        return '\n'.join(lines)
    lines.append(f"- 总成本：{result['spent']}/{result['budget']}；总抽样行数：{result['total_rows']}")
    lines.append(f"- 先导设计方差：{result['variance_num']}/{result['variance_den']}；"
                 f"层覆盖率极差：{result['coverage_gap_num']}/{result['coverage_gap_den']}")
    lines.append('')
    lines.append('| 层 | N | n | 单行成本 | 先导方差 | 入样率 n/N | 实际名单 |')
    lines.append('| --- | --- | --- | --- | --- | --- | --- |')
    for sid, sel in result['selected'].items():
        lines.append(f"| {sid} | {sel['N']} | {sel['n']} | {sel['cost_per_row']} | "
                     f"{sel['pilot_variance_num']}/{sel['pilot_variance_den']} | "
                     f"{sel['inclusion_num']}/{sel['inclusion_den']} | {', '.join(sel['ids'])} |")
    lines.append('')
    lines.append('n/N 是冻结设计在均匀优先序模型下的事前入样率；固定种子只用于重现这一次'
                 '样本名单，已冻结名单在事后不再具有随机性。')
    optimality = result['optimality']
    lines.append(f"最优性依据：{optimality['method']}（{optimality['certified']}），"
                 f"步数 {optimality['steps']} ≤ 上限 {optimality['step_limit']}；"
                 f"平局规则：{' → '.join(optimality['tie_break_order'])}。")
    lines.append(f"来源：frame_digest={provenance['frame_digest']} "
                 f"plan_digest={provenance['plan_digest']} seed={provenance['priority_seed']}")
    return '\n'.join(lines)
