"""Freeze synthetic source-update forecasts, evaluate manual poll schedules and
search globally optimal finite-horizon poll plans.

Everything here is offline: no external endpoint is contacted.  Probabilities
stay exact :class:`fractions.Fraction` values end to end.
"""

import hashlib
import json
from fractions import Fraction
from functools import lru_cache
from math import lcm
from pathlib import Path

from .store import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS poll_forecast (
 forecast_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batch(id),
 digest TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS poll_manual_plan (
 plan_id TEXT PRIMARY KEY, forecast_id TEXT NOT NULL REFERENCES poll_forecast(forecast_id),
 digest TEXT NOT NULL, raw BLOB NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS poll_opt_plan (
 plan_id TEXT PRIMARY KEY, forecast_id TEXT NOT NULL REFERENCES poll_forecast(forecast_id),
 contract TEXT NOT NULL, digest TEXT NOT NULL, raw BLOB NOT NULL, result TEXT NOT NULL);
"""

CONTRACTS = ('min_expected_stale', 'min_max_gap')
DEFAULT_NODE_LIMIT = 10_000_000


def _reject_constant(value):
    raise ValueError('poll_non_finite')


def _load_json(raw):
    # parse_constant rejects NaN / Infinity tokens so every number stays finite.
    return json.loads(raw, parse_constant=_reject_constant)


def _nonnegative(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('poll_bad_integer')
    return value


def _probability(spec):
    num = _nonnegative(spec['num'])
    den = _nonnegative(spec['den'])
    if den == 0 or num > den:
        raise ValueError('poll_bad_probability')
    return Fraction(num, den)


def _frac_pair(value):
    return [value.numerator, value.denominator]


def _as_units(value, scale):
    """Exact conversion of a Fraction to common integer units; never truncates."""
    scaled = value * scale
    if scaled.denominator != 1:
        raise AssertionError('poll_scale_not_integral')
    return scaled.numerator


def record(db_path, input_path):
    raw = Path(input_path).read_bytes()
    doc = _load_json(raw)
    if not doc['forecast_id'] or not doc['slots'] or not doc['feeds']:
        raise ValueError('poll_empty_forecast')
    budget = _nonnegative(doc['total_poll_budget'])
    capacity = _nonnegative(doc['max_polls_per_slot'])
    if budget == 0 or capacity == 0:
        raise ValueError('poll_no_capacity')
    feed_ids = set()
    for feed in doc['feeds']:
        if not feed['id'] or feed['id'] in feed_ids:
            raise ValueError('poll_duplicate_feed')
        feed_ids.add(feed['id'])
        _nonnegative(feed['weight'])
        if len(feed['change_probability']) != len(doc['slots']):
            raise ValueError('poll_bad_horizon')
        for probability in feed['change_probability']:
            _probability(probability)
    if len(set(doc['slots'])) != len(doc['slots']):
        raise ValueError('poll_duplicate_slot')
    if 'slot_capacities' in doc:
        caps = doc['slot_capacities']
        if len(caps) != len(doc['slots']) or any(
                isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in caps):
            raise ValueError('poll_bad_slot_capacities')
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        batch = db.execute('SELECT config_raw FROM batch WHERE id=?',
                           (doc['batch_id'],)).fetchone()
        if not batch:
            raise ValueError('poll_unknown_batch')
        if json.loads(batch['config_raw'])['rule_version'] != doc['rule_version']:
            raise ValueError('poll_wrong_rule_version')
        prior = db.execute('SELECT digest FROM poll_forecast WHERE forecast_id=?',
                           (doc['forecast_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('poll_forecast_conflict')
            return doc['forecast_id']
        with db:
            db.execute('INSERT INTO poll_forecast VALUES (?,?,?,?)',
                       (doc['forecast_id'], doc['batch_id'], digest, raw))
        return doc['forecast_id']
    finally:
        db.close()


def evaluate_manual(db_path, input_path):
    raw = Path(input_path).read_bytes()
    plan = _load_json(raw)
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        prior = db.execute('SELECT digest,result FROM poll_manual_plan WHERE plan_id=?',
                           (plan['plan_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('poll_plan_conflict')
            return json.loads(prior['result'])
        row = db.execute('SELECT raw FROM poll_forecast WHERE forecast_id=?',
                         (plan['forecast_id'],)).fetchone()
        if not row:
            raise ValueError('poll_unknown_forecast')
        doc = _load_json(row['raw'])
        if len(plan['polls']) != len(doc['slots']):
            raise ValueError('poll_bad_schedule_length')
        feeds = {feed['id']: feed for feed in doc['feeds']}
        ordered = sorted(feeds)
        probs = [[_probability(feed['change_probability'][i])
                  for i in range(len(doc['slots']))]
                 for feed in (feeds[key] for key in ordered)]
        products = _survival_products(probs, len(doc['slots']))
        stale = {key: Fraction(0) for key in feeds}
        last = {key: 0 for key in feeds}
        total_expected = Fraction(0)
        max_gap = 0
        used = 0
        feasible = True
        reasons = []
        evidence = []
        for i, selected in enumerate(plan['polls'], 1):
            if len(selected) != len(set(selected)) or any(key not in feeds for key in selected):
                raise ValueError('poll_unknown_or_duplicate_feed')
            used += len(selected)
            if len(selected) > doc['max_polls_per_slot']:
                feasible = False
                reasons.append(doc['slots'][i - 1] + ':capacity')
            slot_evidence = {}
            for j, key in enumerate(ordered):
                feed = feeds[key]
                if key in selected:
                    max_gap = max(max_gap, i - last[key])
                    last[key] = i
                    stale[key] = Fraction(0)
                else:
                    # q_t = 1 - (1-q_{t-1})(1-p_t), evaluated from the last
                    # poll slot so a certain p == 1 update cannot trap q at 1.
                    stale[key] = _stale(products, j, i, last[key])
                total_expected += feed['weight'] * stale[key]
                slot_evidence[key] = [stale[key].numerator, stale[key].denominator]
            evidence.append({'slot': doc['slots'][i - 1], 'stale_probabilities': slot_evidence})
        if used > doc['total_poll_budget']:
            feasible = False
            reasons.append('budget')
        if set(plan['polls'][-1]) != set(feeds):
            feasible = False
            reasons.append('closing_poll_missing')
        result = {'forecast_id': plan['forecast_id'], 'plan_id': plan['plan_id'],
                  'feasible': feasible, 'poll_count': used, 'max_gap': max_gap,
                  'expected_stale_num': total_expected.numerator,
                  'expected_stale_den': total_expected.denominator,
                  'evidence': evidence, 'reasons': reasons}
        with db:
            db.execute('INSERT INTO poll_manual_plan VALUES (?,?,?,?,?)',
                       (plan['plan_id'], plan['forecast_id'], digest, raw,
                        json.dumps(result, sort_keys=True)))
        return result
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Exact finite-horizon optimization
# --------------------------------------------------------------------------- #


def _normalized(doc):
    """Return feeds in canonical feed-id order with exact Fraction probabilities."""
    feeds = sorted(doc['feeds'], key=lambda feed: feed['id'])
    horizon = len(doc['slots'])
    probs = [[_probability(spec) for spec in feed['change_probability']] for feed in feeds]
    weights = [feed['weight'] for feed in feeds]
    ids = [feed['id'] for feed in feeds]
    return ids, weights, probs, horizon


def _survival_products(probs, horizon):
    # R[i][t] = product over slots 1..t of (1 - p_i,t); R[i][0] == 1.
    products = []
    for row in probs:
        values = [Fraction(1)]
        for t in range(horizon):
            values.append(values[-1] * (1 - row[t]))
        products.append(values)
    return products


def _stale(products, feed_index, slot, last_poll):
    """Probability of an unseen change at slot end given the last poll slot.

    Equivalent to the q_t = 1 - (1-q_{t-1})(1-p_t) recurrence but written
    directly against survival products, so it stays exact even when an
    intervening p == 1 makes every later survival product zero.  The difference
    form (R(last) - R(slot)) / R(last) is a single exact fraction and never
    divides zero by zero.
    """
    if slot == last_poll:
        return Fraction(0)
    base = products[feed_index][last_poll]
    if base == 0:  # a certain update (p == 1) already happened after the poll
        return Fraction(1)
    return (base - products[feed_index][slot]) / base


def _prefix_sequence(node):
    sequence = []
    while node[2] is not None:
        sequence.append(node[3])
        node = node[2]
    sequence.reverse()
    return tuple(sequence)


def _simulate_masks(ids, weights, products, horizon, sequence):
    """Independent recurrence replay for a concrete poll-mask sequence.

    q_t = 1 - (1 - q_{t-1})(1 - p_t), reset to zero when the feed is polled at
    the slot end.  This routine deliberately does not trust any precomputed DP
    number; it is the verification code path as well.
    """
    n_feeds = len(ids)
    last = [0] * n_feeds
    feed_gap = [0] * n_feeds
    cumulative = Fraction(0)
    evidence = []
    used = 0
    max_gap = 0
    for t in range(1, horizon + 1):
        mask = sequence[t - 1]
        slot_stale = {}
        for i in range(n_feeds):
            # Independent replay of q_t = 1 - (1-q_{t-1})(1-p_t), expressed
            # from the last poll slot so a p == 1 event cannot trap the value
            # at 1 after a subsequent poll.
            q_value = Fraction(0) if mask & (1 << i) else \
                _stale(products, i, t, last[i])
            if mask & (1 << i):
                gap = t - last[i]
                feed_gap[i] = max(feed_gap[i], gap)
                max_gap = max(max_gap, gap)
                last[i] = t
            cumulative += weights[i] * q_value
            slot_stale[ids[i]] = _frac_pair(q_value)
        used += bin(mask).count('1')
        evidence.append({'slot_index': t, 'stale_probabilities': slot_stale,
                         'cumulative_exposure': _frac_pair(cumulative)})
    total = cumulative
    return {'evidence': evidence, 'expected_stale': total, 'max_gap': max_gap,
            'feed_max_gap': dict(zip(ids, feed_gap)), 'poll_count': used,
            'closing': {ids[i] for i in range(n_feeds)
                        if sequence[-1] & (1 << i)}}


def _solve(doc, node_limit=DEFAULT_NODE_LIMIT):
    """Exhaustive finite-horizon dynamic program.

    DP state after slot t: (last poll slot per feed, polls used so far).  The
    stale probability is a deterministic function of the last poll slot, so a
    state only needs Pareto-optimal (exposure, max gap) labels with predecessor
    pointers.  Both target contracts are served from one run: no single
    lex-ordered label set can retain the exposure-best *and* the gap-best plan
    when they trade off inside the same state.

    Nodes are immutable tuples ``(exposure, gap, parent, mask)``.  When every
    exact exposure fits one common denominator (true for the supported finite
    horizons), exposure is carried as an integer in that unit so the hot loop
    does no Fraction arithmetic; the value is converted back to an exact
    Fraction only when a result is built.
    """
    ids, weights, probs, horizon = _normalized(doc)
    n_feeds = len(ids)
    capacity = doc['max_polls_per_slot']
    budget = doc['total_poll_budget']
    # Optional explicit per-slot caps; default everywhere to the uniform cap.
    caps = doc.get('slot_capacities') or [capacity] * horizon
    products = _survival_products(probs, horizon)
    full_mask = (1 << n_feeds) - 1

    reasons = []
    if budget < n_feeds:
        reasons.append('budget_cannot_close_all_feeds')
    if len(caps) != horizon or any(not isinstance(v, int) or isinstance(v, bool)
                                   or v < 0 for v in caps):
        raise ValueError('poll_bad_slot_capacities')
    if caps[-1] < n_feeds:
        # The closing slot must poll every feed; a final-slot cap below the
        # feed count makes every plan infeasible.
        reasons.append('closing_slot_capacity_insufficient')
    if reasons:
        return {'status': 'infeasible', 'reasons': reasons, 'transitions': 0,
                'states': 1, 'node_limit': node_limit, 'stopped_at_slot': 0,
                'scale': None}

    # cost[i][t][last] = weight_i * stale probability of feed i at slot t when
    # its last poll was at slot `last`.
    frac_cost = []
    scale = 1
    for i in range(n_feeds):
        rows = [[Fraction(0)] * (horizon + 1) for _ in range(horizon + 1)]
        for t in range(1, horizon + 1):
            for last in range(t):
                value = weights[i] * _stale(products, i, t, last)
                rows[t][last] = value
                scale = lcm(scale, value.denominator)
        frac_cost.append(rows)
    cost = [[[0] * (horizon + 1) for _ in range(horizon + 1)]
            for _ in range(n_feeds)]
    for i in range(n_feeds):
        for t in range(1, horizon + 1):
            for last in range(t):
                cost[i][t][last] = _as_units(frac_cost[i][t][last], scale)
    bit_count = {mask: bin(mask).count('1') for mask in range(full_mask + 1)}
    choices_per_slot = []
    for t in range(1, horizon + 1):
        if t == horizon:
            choices_per_slot.append([full_mask])  # mandatory closing poll
        else:
            choices_per_slot.append([m for m in range(full_mask + 1)
                                     if bit_count[m] <= caps[t - 1]])

    # Per slot, the transition for a state and mask depends only on (lasts,
    # mask); precompute it once per state instead of once per candidate.
    root = (0, 0, None, 0)
    root_bucket = [None] * (horizon + 1)
    root_bucket[0] = root
    layer = {(tuple([0] * n_feeds), 0): root_bucket}
    transitions = 0
    state_count = 1
    status = 'optimal'
    stopped_at = horizon
    width = horizon + 1

    for t in range(1, horizon + 1):
        next_layer = {}
        choices = choices_per_slot[t - 1]
        # Precompute the transition table for this slot, indexed by lasts.
        slot_cost = [cost[i][t] for i in range(n_feeds)]
        exhausted = False
        for (lasts, used), labels in layer.items():
            moves = []
            for mask in choices:
                poll_count = bit_count[mask]
                if used + poll_count > budget:
                    continue
                added = 0
                gap_delta = 0
                new_lasts = list(lasts)
                for i in range(n_feeds):
                    if mask & (1 << i):
                        delta = t - lasts[i]
                        if delta > gap_delta:
                            gap_delta = delta
                        new_lasts[i] = t
                    else:
                        added += slot_cost[i][lasts[i]]
                moves.append((poll_count, added, gap_delta, tuple(new_lasts), mask))
            for node in (n for n in labels if n is not None):
                node_exp, node_gap, _, _ = node
                for poll_count, added, gap_delta, new_lasts, mask in moves:
                    transitions += 1
                    if transitions > node_limit:
                        status = 'indeterminate'
                        exhausted = True
                        break
                    candidate = (node_exp + added,
                                 node_gap if node_gap >= gap_delta else gap_delta,
                                 node, mask)
                    key = (new_lasts, used + poll_count)
                    bucket = next_layer.get(key)
                    if bucket is None:
                        bucket = [None] * width
                        next_layer[key] = bucket
                    if _insert_label(bucket, candidate):
                        state_count += 1
                if exhausted:
                    break
            if exhausted:
                break
        if exhausted:
            stopped_at = t - 1
            break
        layer = next_layer
        if not layer:
            return {'status': 'infeasible',
                    'reasons': ['no_feasible_closing_schedule'],
                    'transitions': transitions, 'states': state_count,
                    'node_limit': node_limit, 'stopped_at_slot': t,
                    'scale': scale}

    def finish(node):
        sequence = _prefix_sequence(node)
        simulation = _simulate_masks(ids, weights, products, horizon, sequence)
        # Internal cross-check: the integer-scaled DP accumulation and the
        # independent Fraction recurrence replay must agree exactly.
        assert simulation['expected_stale'] == Fraction(node[0], scale)
        assert simulation['max_gap'] == node[1]
        return sequence, simulation

    result = {'status': status, 'reasons': [], 'transitions': transitions,
              'states': state_count, 'node_limit': node_limit,
              'stopped_at_slot': stopped_at, 'ids': ids, 'slots': doc['slots'],
              'capacity': capacity, 'budget': budget, 'weights': weights,
              'probs': probs, 'scale': scale}

    if status == 'optimal':
        winners = {}
        for contract in CONTRACTS:
            node = _select(layer, contract)
            sequence, simulation = finish(node)
            winners[contract] = {'sequence': sequence, 'simulation': simulation,
                                 'scaled_exposure': node[0]}
        result['winners'] = winners
        return result

    result.update(_indeterminate_bounds(layer, stopped_at, ids, weights, probs,
                                        products, horizon, budget, caps[-1], scale))
    return result


def _chain_lex(left, right):
    """Lexicographic comparison of two node prefix-mask sequences.

    Sequences are compared slot by slot (earliest slot first, which is the
    required stable break order); within a slot the feed-id bit order applies.
    Walks parent links instead of materializing both prefixes.
    """
    left_chain = []
    cursor = left
    while cursor[2] is not None:
        left_chain.append(cursor[3])
        cursor = cursor[2]
    right_chain = []
    cursor = right
    while cursor[2] is not None:
        right_chain.append(cursor[3])
        cursor = cursor[2]
    left_chain.reverse()
    right_chain.reverse()
    if left_chain < right_chain:
        return -1
    if left_chain > right_chain:
        return 1
    return 0


def _insert_label(bucket, candidate):
    """Insert into the Pareto frontier of one DP state.

    ``bucket[g]`` holds the single minimum-exposure label at exact gap ``g``
    (gap is an integer in 0..horizon): among labels with an identical gap only
    the lowest exposure can survive Pareto dominance, with the stable
    slot/feed-id prefix order breaking exact ties.  A label whose gap is no
    smaller and exposure no lower than another is discarded.  Every check
    touches at most horizon + 1 integer-indexed slots.
    """
    c_exp, c_gap = candidate[0], candidate[1]
    for g in range(0, c_gap + 1):
        incumbent = bucket[g]
        if incumbent is None:
            continue
        if incumbent[0] < c_exp or (incumbent[0] == c_exp
                                    and (g < c_gap or _chain_lex(incumbent, candidate) <= 0)):
            return False
    for g in range(c_gap + 1, len(bucket)):
        incumbent = bucket[g]
        if incumbent is not None and (c_exp < incumbent[0]
                                      or (c_exp == incumbent[0]
                                          and _chain_lex(candidate, incumbent) < 0)):
            bucket[g] = None
    bucket[c_gap] = candidate
    return True


def _iter_labels(layer):
    for bucket in layer.values():
        for node in bucket:
            if node is not None:
                yield node


def _select(layer, contract):
    candidates = list(_iter_labels(layer))

    def ranking(node):
        sequence = _prefix_sequence(node)
        if contract == 'min_expected_stale':
            return (node[0], node[1], sequence)
        return (node[1], node[0], sequence)

    return min(candidates, key=ranking)


def _indeterminate_bounds(layer, frontier_slot, ids, weights, probs, products,
                          horizon, budget, closing_cap, scale):
    """Proven bounds when the transition budget is exhausted before completion.

    Bounds use the same integer common-unit exposure as the DP and are returned
    as exact Fractions by the caller-facing layer.
    """
    n_feeds = len(ids)
    full_mask = (1 << n_feeds) - 1

    @lru_cache(maxsize=None)
    def tail_exposure(feed, pos, last_poll, polls_left):
        """Single-feed relaxed tail optimum ignoring cross-feed competition."""
        if polls_left <= 0:
            return None
        if pos == horizon:  # closing slot: this feed must be polled
            return 0
        skip = _as_units(weights[feed] * _stale(products, feed, pos, last_poll), scale)
        rest_skip = tail_exposure(feed, pos + 1, last_poll, polls_left)
        best = None if rest_skip is None else skip + rest_skip
        rest_poll = tail_exposure(feed, pos + 1, pos, polls_left - 1)
        if rest_poll is not None and (best is None or rest_poll < best):
            best = rest_poll
        return best

    lower_exp = None
    lower_gap = None
    incumbent = None
    for (lasts, used), labels in layer.items():
        remaining = budget - used
        for node in (n for n in labels if n is not None):
            node_exp, node_gap = node[0], node[1]
            relaxed = 0
            relaxed_gap = node_gap
            for i in range(n_feeds):
                tail = tail_exposure(i, frontier_slot + 1, lasts[i], remaining)
                if tail is not None:
                    relaxed += tail
                relaxed_gap = max(relaxed_gap, frontier_slot + 1 - lasts[i])
            bound_exp = node_exp + relaxed
            if lower_exp is None or bound_exp < lower_exp:
                lower_exp = bound_exp
            if lower_gap is None or relaxed_gap < lower_gap:
                lower_gap = relaxed_gap
            # Upper bound: hold every feed until the mandatory closing slot.
            if remaining >= n_feeds and closing_cap >= n_feeds:
                sequence = _prefix_sequence(node) + (0,) * (horizon - frontier_slot - 1) \
                    + (full_mask,)
                simulation = _simulate_masks(ids, weights, products, horizon, sequence)
                candidate = {'sequence': sequence, 'simulation': simulation,
                             'scaled_exp': _as_units(simulation['expected_stale'], scale),
                             'gap': simulation['max_gap']}
                if incumbent is None or (candidate['scaled_exp'], candidate['gap']) < \
                        (incumbent['scaled_exp'], incumbent['gap']):
                    incumbent = candidate

    return {'bounds': {
                'expected_stale_lower': (_frac_pair(Fraction(lower_exp, scale))
                                         if lower_exp is not None else None),
                'expected_stale_upper': (_frac_pair(Fraction(incumbent['scaled_exp'], scale))
                                         if incumbent is not None else None),
                'max_gap_lower': lower_gap,
                'max_gap_upper': incumbent['gap'] if incumbent is not None else None},
            'incumbent': incumbent}


# --------------------------------------------------------------------------- #
# Frozen plan records and independent verification
# --------------------------------------------------------------------------- #


def _feeds_summary(doc):
    summary = []
    for feed in sorted(doc['feeds'], key=lambda item: item['id']):
        summary.append({'id': feed['id'], 'weight': feed['weight'],
                        'change_probability': [_frac_pair(_probability(spec))
                                               for spec in feed['change_probability']]})
    return summary


def _build_result(doc, request, solution, contract):
    forecast_id = doc['forecast_id']
    base = {'plan_id': request['plan_id'], 'forecast_id': forecast_id,
            'contract': contract, 'status': solution['status'],
            'reasons': solution['reasons'], 'budget': doc['total_poll_budget'],
            'capacity': doc['max_polls_per_slot'],
            'slot_capacities': doc.get('slot_capacities'),
            'slots': doc['slots'], 'feeds_summary': _feeds_summary(doc),
            'optimality': {'method': 'exhaustive_finite_horizon_dp',
                           'transitions_scored': solution['transitions'],
                           'states_kept': solution['states'],
                           'node_limit': solution['node_limit'],
                           'tie_break': 'slot_order_then_feed_id_order',
                           'dominance_pruning': 'same_last_poll_and_budget_state'}}
    if solution['status'] == 'infeasible':
        base['feasible'] = False
        base['polls'] = []
        return base

    if solution['status'] == 'optimal':
        chosen = solution['winners'][contract]
    else:
        chosen = solution.get('incumbent')
        base['optimality']['bounds'] = solution['bounds']
        if chosen is None:
            base['feasible'] = False
            base['polls'] = []
            base['reasons'] = base['reasons'] + ['no_feasible_plan_within_node_limit']
            return base

    sequence = chosen['sequence']
    simulation = chosen['simulation']
    base['feasible'] = True
    base['polls'] = _polls_from_doc(doc, sequence)
    base['tie_break_key'] = list(sequence)
    base['poll_count'] = simulation['poll_count']
    base['max_gap'] = simulation['max_gap']
    base['feed_max_gap'] = simulation['feed_max_gap']
    base['expected_stale_num'] = simulation['expected_stale'].numerator
    base['expected_stale_den'] = simulation['expected_stale'].denominator
    evidence = []
    for slot, item in zip(doc['slots'], simulation['evidence']):
        evidence.append({'slot': slot,
                         'stale_probabilities': item['stale_probabilities'],
                         'cumulative_exposure': item['cumulative_exposure']})
    base['slot_evidence'] = evidence
    if solution['status'] == 'indeterminate':
        base['reasons'] = ['node_limit_exhausted_not_proven_optimal']
    return base


def _polls_from_doc(doc, sequence):
    ids = sorted(feed['id'] for feed in doc['feeds'])
    return [[ids[i] for i in range(len(ids)) if mask & (1 << i)]
            for mask in sequence]


def optimize(db_path, input_path):
    raw = Path(input_path).read_bytes()
    request = _load_json(raw)
    for key in ('plan_id', 'forecast_id', 'contract'):
        if not request.get(key):
            raise ValueError('poll_bad_optimize_request:' + key)
    if request['contract'] not in CONTRACTS:
        raise ValueError('poll_unknown_contract')
    node_limit = request.get('optimization_budget', DEFAULT_NODE_LIMIT)
    _nonnegative(node_limit)
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        prior = db.execute('SELECT digest,result FROM poll_opt_plan WHERE plan_id=?',
                           (request['plan_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('poll_plan_conflict')
            return json.loads(prior['result'])
        row = db.execute('SELECT raw,digest FROM poll_forecast WHERE forecast_id=?',
                         (request['forecast_id'],)).fetchone()
        if not row:
            raise ValueError('poll_unknown_forecast')
        if hashlib.sha256(row['raw']).hexdigest() != row['digest']:
            raise ValueError('poll_forecast_digest_broken')
        doc = _load_json(row['raw'])
        solution = _solve(doc, node_limit)
        result = _build_result(doc, request, solution, request['contract'])
        with db:
            db.execute('INSERT INTO poll_opt_plan VALUES (?,?,?,?,?,?)',
                       (request['plan_id'], request['forecast_id'],
                        request['contract'], digest, raw,
                        json.dumps(result, sort_keys=True)))
        return result
    finally:
        db.close()


def verify_plan(db_path, input_path):
    """Recompute everything from the frozen forecast bytes, never trusting
    stored q values, then re-derive global optimality with the exact DP."""
    request = _load_json(Path(input_path).read_bytes())
    if not request.get('plan_id'):
        raise ValueError('poll_bad_verify_request:plan_id')
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        plan_row = db.execute('SELECT * FROM poll_opt_plan WHERE plan_id=?',
                              (request['plan_id'],)).fetchone()
        if not plan_row:
            raise ValueError('poll_unknown_plan')
        problems = []
        if hashlib.sha256(plan_row['raw']).hexdigest() != plan_row['digest']:
            problems.append('poll_verify:plan_digest')
        stored = json.loads(plan_row['result'])
        forecast = db.execute('SELECT * FROM poll_forecast WHERE forecast_id=?',
                              (plan_row['forecast_id'],)).fetchone()
        if not forecast:
            raise ValueError('poll_unknown_forecast')
        if hashlib.sha256(forecast['raw']).hexdigest() != forecast['digest']:
            problems.append('poll_verify:forecast_digest')
        doc = _load_json(forecast['raw'])
        ids, weights, probs, horizon = _normalized(doc)
        id_bits = {key: i for i, key in enumerate(ids)}
        products = _survival_products(probs, horizon)

        sequence = []
        if stored.get('polls') and len(stored['polls']) == horizon:
            for slot_polls in stored['polls']:
                if len(slot_polls) != len(set(slot_polls)):
                    problems.append('poll_verify:duplicate_feed_in_slot')
                unknown = [key for key in slot_polls if key not in id_bits]
                if unknown:
                    problems.append('poll_verify:unknown_feed:' + ','.join(unknown))
                mask = 0
                for key in slot_polls:
                    if key in id_bits:
                        mask |= 1 << id_bits[key]
                sequence.append(mask)
        else:
            problems.append('poll_verify:schedule_length')
            sequence = [0] * horizon

        # Resource constraints.
        caps = doc.get('slot_capacities') or [doc['max_polls_per_slot']] * horizon
        used = sum(bin(mask).count('1') for mask in sequence)
        if used > doc['total_poll_budget']:
            problems.append('poll_verify:budget_exceeded')
        for t, mask in enumerate(sequence, 1):
            if bin(mask).count('1') > caps[t - 1]:
                problems.append('poll_verify:capacity_exceeded:slot%d' % t)
        if sequence[-1] != (1 << len(ids)) - 1:
            problems.append('poll_verify:closing_poll_missing')

        simulation = _simulate_masks(ids, weights, products, horizon, sequence)

        # Recurrence check against the stored per-slot evidence.
        stored_evidence = stored.get('slot_evidence', [])
        if len(stored_evidence) != horizon:
            problems.append('poll_verify:evidence_length')
        for t, item in enumerate(simulation['evidence']):
            if t >= len(stored_evidence):
                break
            recorded = stored_evidence[t]
            if recorded.get('slot') != doc['slots'][t]:
                problems.append('poll_verify:slot_label:%d' % (t + 1))
            for key in ids:
                if recorded.get('stale_probabilities', {}).get(key) != \
                        item['stale_probabilities'][key]:
                    problems.append('poll_verify:stale_product:%s:%s'
                                    % (doc['slots'][t], key))
            if recorded.get('cumulative_exposure') != item['cumulative_exposure']:
                problems.append('poll_verify:cumulative_exposure:%s' % doc['slots'][t])
        if (stored.get('expected_stale_num'), stored.get('expected_stale_den')) != \
                (simulation['expected_stale'].numerator,
                 simulation['expected_stale'].denominator):
            problems.append('poll_verify:expected_stale_total')
        if stored.get('max_gap') != simulation['max_gap']:
            problems.append('poll_verify:max_gap')
        if stored.get('feed_max_gap') != simulation['feed_max_gap']:
            problems.append('poll_verify:feed_max_gap')
        if stored.get('poll_count') != simulation['poll_count']:
            problems.append('poll_verify:poll_count')

        # Independent optimality re-derivation from the forecast raw bytes.
        optimal = False
        optimum = None
        contract = plan_row['contract']
        if contract not in CONTRACTS:
            problems.append('poll_verify:unknown_contract:' + str(contract))
        re_solved = _solve(doc, DEFAULT_NODE_LIMIT)
        if contract in CONTRACTS and re_solved['status'] == 'optimal':
            winner = re_solved['winners'][contract]
            optimum = {'expected_stale': _frac_pair(winner['simulation']['expected_stale']),
                       'max_gap': winner['simulation']['max_gap'],
                       'sequence': list(winner['sequence'])}
            if stored.get('status') == 'optimal':
                optimal = (winner['sequence'] == tuple(sequence)
                           and winner['simulation']['expected_stale']
                           == simulation['expected_stale']
                           and winner['simulation']['max_gap'] == simulation['max_gap'])
                if not optimal:
                    problems.append('poll_verify:not_global_optimum')
            else:
                problems.append('poll_verify:stored_plan_not_proven_optimal')
        elif contract in CONTRACTS:
            problems.append('poll_verify:optimizer_indeterminate')

        return {'plan_id': request['plan_id'],
                'forecast_id': plan_row['forecast_id'],
                'contract': plan_row['contract'],
                'status': stored.get('status'),
                'problems': sorted(set(problems)),
                'optimal': optimal,
                'recomputed_expected_stale': _frac_pair(simulation['expected_stale']),
                'recomputed_max_gap': simulation['max_gap'],
                'recomputed_feed_max_gap': simulation['feed_max_gap'],
                'recomputed_poll_count': simulation['poll_count'],
                'proven_optimum': optimum}
    finally:
        db.close()
