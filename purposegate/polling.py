"""Freeze synthetic source-update forecasts and evaluate a manual poll schedule.

No optimal poll schedule is searched here; no external endpoint is contacted.
"""

import hashlib
import json
from fractions import Fraction
from pathlib import Path

from .store import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS poll_forecast (
 forecast_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batch(id),
 digest TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS poll_manual_plan (
 plan_id TEXT PRIMARY KEY, forecast_id TEXT NOT NULL REFERENCES poll_forecast(forecast_id),
 digest TEXT NOT NULL, raw BLOB NOT NULL, result TEXT NOT NULL);
"""


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


def record(db_path, input_path):
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw)
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
    plan = json.loads(raw)
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
        doc = json.loads(row['raw'])
        if len(plan['polls']) != len(doc['slots']):
            raise ValueError('poll_bad_schedule_length')
        feeds = {feed['id']: feed for feed in doc['feeds']}
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
            for key, feed in feeds.items():
                p = _probability(feed['change_probability'][i - 1])
                stale[key] = 1 - (1 - stale[key]) * (1 - p)
                if key in selected:
                    max_gap = max(max_gap, i - last[key])
                    last[key] = i
                    stale[key] = Fraction(0)
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
