"""Optimal poll planning: contracts, shared-budget scheduling, boundaries,
version/idempotency conflicts and independent optimality verification."""

import json
from fractions import Fraction
from itertools import product
from pathlib import Path

import pytest

from purposegate.ingest import initialize
from purposegate.polling import (
    CONTRACTS,
    _normalized,
    _survival_products,
    evaluate_manual,
    optimize,
    record,
    verify_plan,
)
from purposegate.store import connect

FIX = Path(__file__).resolve().parent.parent / 'fixtures'


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / 'ledger.db'
    initialize(path, FIX / 'poll-config.json')
    return path


def _write(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding='utf-8')
    return path


def _brute_force(doc):
    """Independent enumeration of every feasible mask sequence.

    Scoring uses an independent integer common-unit implementation written
    separately from the production DP, so an implementation bug shared by both
    cannot hide: the enumeration walks raw mask sequences and recomputes q from
    each feed's last poll slot.
    """
    from math import lcm

    ids, weights, probs, horizon = _normalized(doc)
    products = _survival_products(probs, horizon)
    n_feeds = len(ids)
    caps = doc.get('slot_capacities') or [doc['max_polls_per_slot']] * horizon
    budget = doc['total_poll_budget']
    full = (1 << n_feeds) - 1

    # Integer exposure costs, recomputed independently of the DP's table.
    scale = 1
    raw_cost = [[[0] * (horizon + 1) for _ in range(horizon + 1)]
                for _ in range(n_feeds)]
    for i in range(n_feeds):
        for t in range(1, horizon + 1):
            for last in range(t):
                value = weights[i] * (1 - products[i][t] / products[i][last]
                                      if products[i][last] != 0 else Fraction(1))
                scale = lcm(scale, value.denominator)
                raw_cost[i][t][last] = value
    unit = [[[0] * (horizon + 1) for _ in range(horizon + 1)]
            for _ in range(n_feeds)]
    for i in range(n_feeds):
        for t in range(1, horizon + 1):
            for last in range(t):
                scaled = raw_cost[i][t][last] * scale
                assert scaled.denominator == 1
                unit[i][t][last] = scaled.numerator

    choices = [[m for m in range(full + 1) if bin(m).count('1') <= caps[t]]
               for t in range(horizon - 1)]
    best_exp = best_gap = None
    feasible = 0
    for combo in product(*choices):
        sequence = combo + (full,)
        if sum(bin(m).count('1') for m in sequence) > budget:
            continue
        feasible += 1
        # Independent integer scoring loop, no production DP calls.
        lasts = [0] * n_feeds
        exposure = 0
        max_gap = 0
        for t, mask in enumerate(sequence, 1):
            for i in range(n_feeds):
                if mask & (1 << i):
                    gap = t - lasts[i]
                    if gap > max_gap:
                        max_gap = gap
                    lasts[i] = t
                else:
                    exposure += unit[i][t][lasts[i]]
        key_exp = (exposure, max_gap, sequence)
        key_gap = (max_gap, exposure, sequence)
        if best_exp is None or key_exp < best_exp[0]:
            best_exp = (key_exp, None)
        if best_gap is None or key_gap < best_gap[0]:
            best_gap = (key_gap, None)
    return feasible, best_exp, best_gap, scale


# --------------------------------------------------------------------------- #
# Group 1: the two contracts yield genuinely different real poll schedules.
# --------------------------------------------------------------------------- #


def test_single_source_contracts_match_acceptance_fractions(db):
    record(db, FIX / 'poll-forecast.json')
    stale = optimize(db, FIX / 'poll-opt-request.json')
    gap = optimize(db, FIX / 'poll-opt-request-gap.json')

    assert stale['status'] == 'optimal'
    assert stale['polls'] == [['consent-feed'], [], [], [], [], ['consent-feed']]
    assert (stale['expected_stale_num'], stale['expected_stale_den']) == (9049, 10000)
    assert stale['max_gap'] == 5
    assert stale['feed_max_gap'] == {'consent-feed': 5}
    assert stale['poll_count'] == 2

    assert gap['status'] == 'optimal'
    assert gap['polls'] == [[], [], ['consent-feed'], [], [], ['consent-feed']]
    assert (gap['expected_stale_num'], gap['expected_stale_den']) == (21, 10)
    assert gap['max_gap'] == 3

    # Same probability evidence, different fresh trajectories: the slot q
    # tracks must differ, not just a relabeled status field.
    assert stale['slot_evidence'][0]['stale_probabilities'] == {'consent-feed': [0, 1]}
    assert gap['slot_evidence'][0]['stale_probabilities'] == {'consent-feed': [9, 10]}
    assert stale['polls'] != gap['polls']


def test_slot_evidence_is_exact_recurrence(db):
    record(db, FIX / 'poll-forecast.json')
    result = optimize(db, FIX / 'poll-opt-request-gap.json')
    q = Fraction(0)
    cumulative = Fraction(0)
    probs = [Fraction(9, 10)] + [Fraction(1, 10)] * 5
    polls = {3, 6}  # the gap contract polls at T3 and T6
    for t, evidence in enumerate(result['slot_evidence'], 1):
        q = 1 - (1 - q) * (1 - probs[t - 1])
        if t in polls:
            # Slot-end poll resets q after the update, so the recorded q is 0.
            q = Fraction(0)
        cumulative += q
        assert evidence['stale_probabilities']['consent-feed'] == \
            [q.numerator, q.denominator]
        assert evidence['cumulative_exposure'] == \
            [cumulative.numerator, cumulative.denominator]


def test_manual_evaluation_still_available_and_consistent(db):
    record(db, FIX / 'poll-forecast.json')
    manual = evaluate_manual(db, FIX / 'poll-manual.json')
    optimal = optimize(db, FIX / 'poll-opt-request-gap.json')
    assert manual['feasible'] is True
    assert (manual['expected_stale_num'], manual['expected_stale_den']) == (21, 10)
    assert manual['max_gap'] == optimal['max_gap'] == 3


# --------------------------------------------------------------------------- #
# Group 2: multiple feeds, shared budget and per-slot capacity competition.
# --------------------------------------------------------------------------- #


def test_multi_feed_shared_budget_schedule_is_globally_optimal(db):
    record(db, FIX / 'poll-forecast-duo.json')
    doc = json.loads((FIX / 'poll-forecast-duo.json').read_text())
    request = _write(db.parent, 'duo.json',
                     {'plan_id': 'opt-D2-stale', 'forecast_id': 'forecast-D2',
                      'contract': 'min_expected_stale'})
    result = optimize(db, request)

    assert result['status'] == 'optimal'
    # Last slot closes every feed; intermediate slots respect the explicit caps.
    caps = doc['slot_capacities']
    assert set(result['polls'][-1]) == {f['id'] for f in doc['feeds']}
    for t, slot_polls in enumerate(result['polls'][:-1]):
        assert len(slot_polls) <= caps[t]
    assert result['poll_count'] <= doc['total_poll_budget']
    assert len(result['feeds_summary']) == 2

    feasible, best_exp, _, scale = _brute_force(doc)
    assert feasible > 0
    assert Fraction(result['expected_stale_num'],
                    result['expected_stale_den']) == \
        Fraction(best_exp[0][0], scale)
    assert result['max_gap'] == best_exp[0][1]
    assert result['tie_break_key'] == list(best_exp[0][2])


def test_multi_feed_min_max_gap_contract(db):
    record(db, FIX / 'poll-forecast-duo.json')
    doc = json.loads((FIX / 'poll-forecast-duo.json').read_text())
    request = _write(db.parent, 'gap.json',
                     {'plan_id': 'opt-D2-gap', 'forecast_id': 'forecast-D2',
                      'contract': 'min_max_gap'})
    result = optimize(db, request)
    _, _, best_gap, scale = _brute_force(doc)
    assert result['max_gap'] == best_gap[0][0]
    assert Fraction(result['expected_stale_num'],
                    result['expected_stale_den']) == \
        Fraction(best_gap[0][1], scale)
    assert result['tie_break_key'] == list(best_gap[0][2])
    # Every feed has its own max-gap accounting from time origin 0.
    assert set(result['feed_max_gap']) == {'feed-delta', 'feed-echo'}


def test_joint_schedule_is_not_per_feed_optima_stitched(db, tmp_path):
    """A feed polled twice inside one slot would be ideal for that feed alone
    but violates the shared capacity; the joint plan must avoid it."""
    record(db, FIX / 'poll-forecast-multi.json')
    result = optimize(db, FIX / 'poll-opt-multi-request.json')
    counts = [len(slot_polls) for slot_polls in result['polls']]
    assert max(counts[:-1]) <= 2
    # The closing slot is the only one allowed to hold all three feeds.
    assert counts[-1] == 3


def test_randomized_small_cases_match_full_enumeration():
    import random
    random.seed(20261118)
    for _ in range(10):
        n_feeds = random.randint(1, 3)
        horizon = random.randint(3, 6)
        feeds = []
        for k in range(n_feeds):
            probabilities = []
            for _ in range(horizon):
                roll = random.random()
                if roll < 0.15:
                    spec = (0, 1)
                elif roll < 0.25:
                    spec = (1, 1)
                else:
                    den = random.choice([2, 3, 4, 5, 6, 10])
                    spec = (random.randint(0, den), den)
                probabilities.append({'num': spec[0], 'den': spec[1]})
            feeds.append({'id': f'f{k}', 'weight': random.randint(0, 4),
                          'change_probability': probabilities})
        doc = {'slots': [f'T{i}' for i in range(horizon)],
               'total_poll_budget': random.randint(n_feeds, n_feeds * horizon),
               'max_polls_per_slot': random.randint(1, n_feeds),
               'slot_capacities': [random.randint(1, n_feeds)
                                   for _ in range(horizon - 1)] + [n_feeds],
               'feeds': feeds}
        feasible, best_exp, best_gap, _ = _brute_force(doc)
        if feasible == 0:
            continue
        from purposegate.polling import _solve
        solution = _solve(doc)
        assert solution['status'] == 'optimal'
        winner_exp = solution['winners']['min_expected_stale']
        winner_gap = solution['winners']['min_max_gap']
        assert winner_exp['sequence'] == best_exp[0][2]
        assert winner_gap['sequence'] == best_gap[0][2]


# --------------------------------------------------------------------------- #
# Group 3: probabilities, capacities, boundaries and no-solution cases.
# --------------------------------------------------------------------------- #


def _forecast(fid, budget, capacity, feeds_n=1, slots=6, caps=None, probs=None):
    probabilities = probs or [[{'num': 1, 'den': 10}] * slots for _ in range(feeds_n)]
    return {'forecast_id': fid, 'batch_id': 'poll-demo', 'rule_version': 'poll-v1',
            'slots': [f'T{i + 1}' for i in range(slots)],
            'total_poll_budget': budget, 'max_polls_per_slot': capacity,
            **({'slot_capacities': caps} if caps else {}),
            'feeds': [{'id': f'feed{k}', 'weight': k + 1,
                       'change_probability': probabilities[k]} for k in range(feeds_n)]}


def test_zero_and_one_probabilities_and_no_update_feed(db, tmp_path):
    doc = _forecast(
        'fx', 4, 2, feeds_n=2, slots=4,
        probs=[[{'num': 0, 'den': 1}] * 4,
               [{'num': 1, 'den': 1}, {'num': 1, 'den': 10},
                {'num': 0, 'den': 1}, {'num': 1, 'den': 2}]])
    record(db, _write(tmp_path, 'fx.json', doc))
    request = _write(tmp_path, 'req.json',
                     {'plan_id': 'p', 'forecast_id': 'fx',
                      'contract': 'min_expected_stale'})
    result = optimize(db, request)
    assert result['status'] == 'optimal'
    # The no-update feed carries zero stale exposure at every slot.
    zero_track = [row['stale_probabilities']['feed0']
                  for row in result['slot_evidence']]
    assert all(pair == [0, 1] for pair in zero_track)
    # p == 1 in slot 1 forces q to 1 until the next poll of that feed.
    assert result['slot_evidence'][0]['stale_probabilities']['feed1'] == [1, 1]

def test_first_and_last_slot_boundaries_are_pollable(db, tmp_path):
    doc = _forecast('boundary', 2, 1, slots=2)
    record(db, _write(tmp_path, 'b.json', doc))
    request = _write(tmp_path, 'req.json',
                     {'plan_id': 'bp', 'forecast_id': 'boundary',
                      'contract': 'min_expected_stale'})
    result = optimize(db, request)
    assert result['polls'] == [['feed0'], ['feed0']]
    assert result['max_gap'] == 1


def test_budget_below_feed_count_is_infeasible(db, tmp_path):
    doc = _forecast('inf-budget', 1, 2, feeds_n=2)
    record(db, _write(tmp_path, 'ib.json', doc))
    request = _write(tmp_path, 'req.json',
                     {'plan_id': 'ib', 'forecast_id': 'inf-budget',
                      'contract': 'min_max_gap'})
    result = optimize(db, request)
    assert result['status'] == 'infeasible'
    assert result['feasible'] is False
    assert 'budget_cannot_close_all_feeds' in result['reasons']


def test_final_slot_capacity_shortfall_is_infeasible(db, tmp_path):
    doc = _forecast('inf-cap', 4, 1, feeds_n=2)
    record(db, _write(tmp_path, 'ic.json', doc))
    request = _write(tmp_path, 'req.json',
                     {'plan_id': 'ic', 'forecast_id': 'inf-cap',
                      'contract': 'min_max_gap'})
    result = optimize(db, request)
    assert result['status'] == 'infeasible'
    assert 'closing_slot_capacity_insufficient' in result['reasons']


def test_explicit_slot_capacity_mismatch_is_rejected(db, tmp_path):
    doc = _forecast('bad-caps', 4, 2, feeds_n=2, caps=[2, 2, 2, 2, 2])
    with pytest.raises(ValueError, match='poll_bad_slot_capacities'):
        record(db, _write(tmp_path, 'bc.json', doc))


def test_indeterminate_result_keeps_proven_bounds_not_optimal_claim(db, tmp_path):
    record(db, FIX / 'poll-forecast-multi.json')
    request = _write(tmp_path, 'lim.json',
                     {'plan_id': 'lim', 'forecast_id': 'forecast-M3',
                      'contract': 'min_expected_stale', 'optimization_budget': 300})
    result = optimize(db, request)
    assert result['status'] == 'indeterminate'
    assert result['reasons'] == ['node_limit_exhausted_not_proven_optimal']
    bounds = result['optimality']['bounds']
    # Proven bounds are present and bracket the true optimum.
    assert bounds['expected_stale_lower'] is not None
    assert bounds['expected_stale_upper'] is not None
    lower = Fraction(*bounds['expected_stale_lower'])
    upper = Fraction(*bounds['expected_stale_upper'])
    assert lower <= upper
    # The incumbent is a real, constraint-respecting plan ...
    assert result['feasible'] is True
    assert result['poll_count'] <= 8
    assert set(result['polls'][-1]) == {'feed-alpha', 'feed-bravo', 'feed-charlie'}
    assert Fraction(result['expected_stale_num'],
                    result['expected_stale_den']) == upper
    # ... but the result refuses to claim global optimality.
    assert result['reasons'] == ['node_limit_exhausted_not_proven_optimal']


def test_invalid_probabilities_and_resources_are_rejected(db, tmp_path):
    base = json.loads((FIX / 'poll-forecast.json').read_text())

    def mutated(**changes):
        doc = json.loads(json.dumps(base))
        doc['forecast_id'] = changes.pop('fid')
        for key, value in changes.items():
            doc[key] = value
        return doc

    bad_prob = mutated(fid='e1')
    bad_prob['feeds'][0]['change_probability'][0] = {'num': 2, 'den': 1}
    with pytest.raises(ValueError, match='poll_bad_probability'):
        record(db, _write(tmp_path, 'e1.json', bad_prob))

    bad_den = mutated(fid='e2')
    bad_den['feeds'][0]['change_probability'][0] = {'num': 1, 'den': 0}
    with pytest.raises(ValueError, match='poll_bad_probability'):
        record(db, _write(tmp_path, 'e2.json', bad_den))

    with pytest.raises(ValueError, match='poll_no_capacity'):
        record(db, _write(tmp_path, 'e3.json', mutated(fid='e3', total_poll_budget=0)))
    with pytest.raises(ValueError, match='poll_no_capacity'):
        record(db, _write(tmp_path, 'e4.json', mutated(fid='e4', max_polls_per_slot=0)))

    duplicate = mutated(fid='e5')
    duplicate['feeds'][0]['id'] = 'x'
    duplicate['feeds'].append(json.loads(json.dumps(duplicate['feeds'][0])))
    with pytest.raises(ValueError, match='poll_duplicate_feed'):
        record(db, _write(tmp_path, 'e5.json', duplicate))

    non_finite = json.dumps(base).replace('9', 'NaN', 1)
    path = tmp_path / 'nan.json'
    path.write_text(non_finite, encoding='utf-8')
    with pytest.raises(ValueError, match='poll_non_finite'):
        record(db, path)


def test_unknown_feed_and_bad_contract_requests(db, tmp_path):
    record(db, FIX / 'poll-forecast.json')
    request = _write(tmp_path, 'u.json',
                     {'plan_id': 'u', 'forecast_id': 'ghost',
                      'contract': 'min_expected_stale'})
    with pytest.raises(ValueError, match='poll_unknown_forecast'):
        optimize(db, request)
    request = _write(tmp_path, 'c.json',
                     {'plan_id': 'c', 'forecast_id': 'forecast-S1',
                      'contract': 'greedy'})
    with pytest.raises(ValueError, match='poll_unknown_contract'):
        optimize(db, request)


# --------------------------------------------------------------------------- #
# Group 4: version conflicts and idempotent frozen identity.
# --------------------------------------------------------------------------- #


def test_same_forecast_id_with_different_text_conflicts(db, tmp_path):
    record(db, FIX / 'poll-forecast.json')
    doc = json.loads((FIX / 'poll-forecast.json').read_text())
    doc['total_poll_budget'] = 3
    with pytest.raises(ValueError, match='poll_forecast_conflict'):
        record(db, _write(tmp_path, 'conflict.json', doc))


def test_record_and_optimize_are_idempotent(db):
    assert record(db, FIX / 'poll-forecast.json') == 'forecast-S1'
    assert record(db, FIX / 'poll-forecast.json') == 'forecast-S1'
    first = optimize(db, FIX / 'poll-opt-request.json')
    second = optimize(db, FIX / 'poll-opt-request.json')
    assert first == second


def test_same_plan_id_different_text_is_rejected_but_old_plan_kept(db, tmp_path):
    record(db, FIX / 'poll-forecast.json')
    optimize(db, FIX / 'poll-opt-request.json')
    conflicting = _write(tmp_path, 'conf.json',
                         {'plan_id': 'opt-S1-stale',
                          'forecast_id': 'forecast-S1',
                          'contract': 'min_max_gap'})
    with pytest.raises(ValueError, match='poll_plan_conflict'):
        optimize(db, conflicting)
    ledger = connect(db)
    row = ledger.execute('SELECT contract FROM poll_opt_plan WHERE plan_id=?',
                         ('opt-S1-stale',)).fetchone()
    ledger.close()
    assert row['contract'] == 'min_expected_stale'


def test_new_rule_version_is_rejected_for_old_batch(db, tmp_path):
    doc = json.loads((FIX / 'poll-forecast.json').read_text())
    doc['forecast_id'] = 'new-rule'
    doc['rule_version'] = 'poll-v2'
    with pytest.raises(ValueError, match='poll_wrong_rule_version'):
        record(db, _write(tmp_path, 'nr.json', doc))


def test_unknown_batch_is_rejected(db, tmp_path):
    doc = json.loads((FIX / 'poll-forecast.json').read_text())
    doc['forecast_id'] = 'new-batch'
    doc['batch_id'] = 'no-such-batch'
    with pytest.raises(ValueError, match='poll_unknown_batch'):
        record(db, _write(tmp_path, 'nb.json', doc))


# --------------------------------------------------------------------------- #
# Group 5: independent optimality recomputation and tamper detection.
# --------------------------------------------------------------------------- #


def test_verify_recomputes_optimum_from_frozen_forecast(db):
    record(db, FIX / 'poll-forecast.json')
    optimize(db, FIX / 'poll-opt-request.json')
    report = verify_plan(db, FIX / 'poll-verify-request.json')
    assert report['problems'] == []
    assert report['optimal'] is True
    assert report['proven_optimum']['sequence'] == [1, 0, 0, 0, 0, 1]
    assert report['proven_optimum']['expected_stale'] == [9049, 10000]
    assert report['recomputed_max_gap'] == 5


def test_verify_detects_substituted_suboptimal_plan(db, tmp_path):
    record(db, FIX / 'poll-forecast.json')
    optimize(db, FIX / 'poll-opt-request.json')
    ledger = connect(db)
    row = ledger.execute('SELECT result FROM poll_opt_plan WHERE plan_id=?',
                         ('opt-S1-stale',)).fetchone()
    result = json.loads(row['result'])
    result['polls'] = [[], [], ['consent-feed'], [], [], ['consent-feed']]
    ledger.execute('UPDATE poll_opt_plan SET result=? WHERE plan_id=?',
                   (json.dumps(result, sort_keys=True), 'opt-S1-stale'))
    ledger.commit()
    ledger.close()
    report = verify_plan(db, FIX / 'poll-verify-request.json')
    assert 'poll_verify:not_global_optimum' in report['problems']
    assert report['optimal'] is False


def test_verify_detects_wrong_product_and_tampered_probability(db, tmp_path):
    record(db, FIX / 'poll-forecast.json')
    optimize(db, FIX / 'poll-opt-request.json')
    verify_path = _write(tmp_path, 'v.json', {'plan_id': 'opt-S1-stale'})

    ledger = connect(db)
    row = ledger.execute('SELECT result FROM poll_opt_plan WHERE plan_id=?',
                         ('opt-S1-stale',)).fetchone()
    result = json.loads(row['result'])
    result['slot_evidence'][0]['stale_probabilities']['consent-feed'] = [1, 2]
    ledger.execute('UPDATE poll_opt_plan SET result=? WHERE plan_id=?',
                   (json.dumps(result, sort_keys=True), 'opt-S1-stale'))
    ledger.commit()
    ledger.close()
    assert any('stale_product' in p for p in verify_plan(db, verify_path)['problems'])

    # Tamper the frozen forecast probability itself: digest check must fire.
    ledger = connect(db)
    raw = ledger.execute('SELECT raw FROM poll_forecast WHERE forecast_id=?',
                         ('forecast-S1',)).fetchone()['raw']
    forecast = json.loads(raw)
    forecast['feeds'][0]['change_probability'][0] = {'num': 1, 'den': 2}
    ledger.execute('UPDATE poll_forecast SET raw=? WHERE forecast_id=?',
                   (json.dumps(forecast).encode(), 'forecast-S1'))
    ledger.commit()
    ledger.close()
    assert 'poll_verify:forecast_digest' in verify_plan(db, verify_path)['problems']


def test_verify_detects_missing_closing_poll_and_digest_tamper(db, tmp_path):
    record(db, FIX / 'poll-forecast.json')
    optimize(db, FIX / 'poll-opt-request.json')
    verify_path = _write(tmp_path, 'v.json', {'plan_id': 'opt-S1-stale'})

    ledger = connect(db)
    row = ledger.execute('SELECT result FROM poll_opt_plan WHERE plan_id=?',
                         ('opt-S1-stale',)).fetchone()
    result = json.loads(row['result'])
    result['polls'] = [['consent-feed'], [], [], [], [], []]
    ledger.execute('UPDATE poll_opt_plan SET result=? WHERE plan_id=?',
                   (json.dumps(result, sort_keys=True), 'opt-S1-stale'))
    ledger.commit()
    ledger.close()
    assert 'poll_verify:closing_poll_missing' in verify_plan(db, verify_path)['problems']

    ledger = connect(db)
    ledger.execute('UPDATE poll_opt_plan SET digest=? WHERE plan_id=?',
                   ('0' * 64, 'opt-S1-stale'))
    ledger.commit()
    ledger.close()
    assert 'poll_verify:plan_digest' in verify_plan(db, verify_path)['problems']


def test_verify_unknown_plan_raises(db, tmp_path):
    path = _write(tmp_path, 'missing.json', {'plan_id': 'never-stored'})
    with pytest.raises(ValueError, match='poll_unknown_plan'):
        verify_plan(db, path)


def test_contracts_are_the_two_fixed_objectives():
    assert CONTRACTS == ('min_expected_stale', 'min_max_gap')
