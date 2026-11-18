"""Freeze synthetic decision aggregates and report their raw group rates.

The raw summary ignores case-mix strata; it is not a stratified diagnostic.

The decide/verify flow freezes a caller-chosen estimation contract (source
digest, batch/rule versions, investigation thresholds, group/stratum order,
estimand definition) before any conclusion is drawn, then computes exact
two-sided conditional p-values with integer combinatorics — no Gaussian
approximation, no simulation, no rounding before threshold comparison.
Conclusions cover only this offline synthetic aggregate; they do not audit
real persons or legal fairness.
"""

import hashlib
import json
from fractions import Fraction
from math import comb
from pathlib import Path

from .store import connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS group_audit_source (
 audit_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batch(id),
 digest TEXT NOT NULL, raw BLOB NOT NULL);
"""

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_audit_contract (
 contract_id TEXT PRIMARY KEY,
 audit_id TEXT NOT NULL REFERENCES group_audit_source(audit_id),
 digest TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS group_audit_diagnostic (
 contract_id TEXT PRIMARY KEY REFERENCES group_audit_contract(contract_id),
 audit_id TEXT NOT NULL, batch_id TEXT NOT NULL, rule_version TEXT NOT NULL,
 source_digest TEXT NOT NULL, contract_digest TEXT NOT NULL,
 estimand TEXT NOT NULL, result_json TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
"""

# Finite exact-inference contract: within these limits every result is exact
# and independently recomputable; beyond them decide() raises a business
# error instead of silently falling back to an approximate p-value.
MIN_STRATA = 2
MAX_STRATA = 6
MAX_STRATUM_TOTAL = 200

ESTIMAND_DEFINITIONS = {
    'observed_mix': ('observed_mix：每组总体允许率按该组在本次源数据中的实际层样本构成加权；'
                     '总体差值混合了层内许可差异与层样本构成差异，不得解释为纯层内许可差异。'),
    'common_reference': ('common_reference：两组各层允许率按合同给定的共同层权重'
                         '（合计为 1 的精确非负有理数）加权；总体差值是共同参考构成下的'
                         '标准化层内许可差异，层构成效应已被排除。'),
}

SCOPE_NOTE = '结论仅适用于这份离线合成汇总；不声称审计了真实人员或法定公平性。'

P_DEFINITION = ('双侧 p 为零假设下 |T|≥|T_observed|（含等号）的概率；'
                '整数组合数/有理数精确计算，阈值比较前不四舍五入。')
TAILS_DEFINITION = 'lower 为 T≤−|T_observed|、upper 为 T≥|T_observed| 的精确尾概率。'


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('group_audit_bad_count')
    return value


def record(db_path, input_path):
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw)
    if not doc['audit_id'] or len(doc['groups']) != 2 or not doc['strata']:
        raise ValueError('group_audit_bad_contract')
    if len(set(doc['groups'])) != 2 or len(set(doc['strata'])) != len(doc['strata']):
        raise ValueError('group_audit_duplicate_label')
    pairs = set()
    for cell in doc['cells']:
        pair = cell['group'], cell['stratum']
        if pair[0] not in doc['groups'] or pair[1] not in doc['strata'] or pair in pairs:
            raise ValueError('group_audit_bad_cell')
        pairs.add(pair)
        _count(cell['allowed'])
        _count(cell['denied'])
        if cell['allowed'] + cell['denied'] == 0:
            raise ValueError('group_audit_empty_cell')
    if pairs != {(g, s) for g in doc['groups'] for s in doc['strata']}:
        raise ValueError('group_audit_incomplete_matrix')
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        batch = db.execute('SELECT config_raw FROM batch WHERE id=?',
                           (doc['batch_id'],)).fetchone()
        if not batch:
            raise ValueError('group_audit_unknown_batch')
        config = json.loads(batch['config_raw'])
        if config['rule_version'] != doc['rule_version']:
            raise ValueError('group_audit_wrong_rule_version')
        prior = db.execute('SELECT digest FROM group_audit_source WHERE audit_id=?',
                           (doc['audit_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('group_audit_source_conflict')
            return doc['audit_id']
        with db:
            db.execute('INSERT INTO group_audit_source VALUES (?,?,?,?)',
                       (doc['audit_id'], doc['batch_id'], digest, raw))
        return doc['audit_id']
    finally:
        db.close()


def raw_summary(db_path, audit_id):
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        row = db.execute('SELECT raw FROM group_audit_source WHERE audit_id=?',
                         (audit_id,)).fetchone()
        if not row:
            raise ValueError('group_audit_unknown_source')
        doc = json.loads(row['raw'])
        result = {}
        for group in doc['groups']:
            cells = [c for c in doc['cells'] if c['group'] == group]
            allowed = sum(c['allowed'] for c in cells)
            total = sum(c['allowed'] + c['denied'] for c in cells)
            rate = Fraction(allowed, total)
            result[group] = {'allowed': allowed, 'total': total,
                             'rate_num': rate.numerator, 'rate_den': rate.denominator}
        a, b = doc['groups']
        gap = Fraction(result[a]['allowed'], result[a]['total']) - Fraction(
            result[b]['allowed'], result[b]['total'])
        return {'audit_id': audit_id, 'groups': result,
                'gap_num': gap.numerator, 'gap_den': gap.denominator}
    finally:
        db.close()


def _rational(value, code='group_audit_bad_contract'):
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) for v in value)):
        raise ValueError(code)
    num, den = value
    if den <= 0 or num < 0:
        raise ValueError(code)
    return Fraction(num, den)


def _parse_contract(doc):
    if not isinstance(doc, dict):
        raise ValueError('group_audit_bad_contract')
    contract_id = doc.get('contract_id')
    audit_id = doc.get('audit_id')
    if (not isinstance(contract_id, str) or not contract_id
            or not isinstance(audit_id, str) or not audit_id):
        raise ValueError('group_audit_bad_contract')
    estimand = doc.get('estimand')
    if estimand not in ESTIMAND_DEFINITIONS:
        raise ValueError('group_audit_unknown_estimand')
    thresholds = doc.get('thresholds')
    if not isinstance(thresholds, dict):
        raise ValueError('group_audit_bad_contract')
    min_gap = _rational(thresholds.get('min_gap'))
    max_p = _rational(thresholds.get('max_p'))
    if min_gap > 1 or max_p > 1:
        raise ValueError('group_audit_bad_contract')
    weights = None
    if estimand == 'common_reference':
        raw_weights = doc.get('weights')
        if not isinstance(raw_weights, dict) or not raw_weights:
            raise ValueError('group_audit_bad_weights')
        weights = {str(k): _rational(v, 'group_audit_bad_weights')
                   for k, v in raw_weights.items()}
    elif 'weights' in doc:
        raise ValueError('group_audit_bad_contract')
    return {'contract_id': contract_id, 'audit_id': audit_id, 'estimand': estimand,
            'min_gap': min_gap, 'max_p': max_p, 'weights': weights}


def _matrix(doc):
    """Re-validate a frozen source document and index its cells."""
    groups = doc['groups']
    strata = doc['strata']
    if not doc['audit_id'] or len(groups) != 2 or not strata:
        raise ValueError('group_audit_bad_contract')
    if len(set(groups)) != 2 or len(set(strata)) != len(strata):
        raise ValueError('group_audit_duplicate_label')
    cells = {}
    for cell in doc['cells']:
        pair = cell['group'], cell['stratum']
        if pair[0] not in groups or pair[1] not in strata or pair in cells:
            raise ValueError('group_audit_bad_cell')
        allowed = _count(cell['allowed'])
        denied = _count(cell['denied'])
        if allowed + denied == 0:
            raise ValueError('group_audit_empty_cell')
        cells[pair] = (allowed, denied)
    if set(cells) != {(g, s) for g in groups for s in strata}:
        raise ValueError('group_audit_incomplete_matrix')
    return groups, strata, cells


def _hg_support(m, n_first, total):
    return max(0, n_first - (total - m)), min(m, n_first)


def _hg_prob(m, total, n_first, k):
    return Fraction(comb(m, k) * comb(total - m, n_first - k), comb(total, n_first))


def _tails(dist, observed):
    """Exact two-sided p plus both tails of a discrete statistic distribution."""
    bound = abs(observed)
    p = lower = upper = Fraction(0)
    for stat, prob in dist.items():
        if abs(stat) >= bound:
            p += prob
        if stat <= -bound:
            lower += prob
        if stat >= bound:
            upper += prob
    return p, lower, upper


def _pooled_test(a0, n0, a1, n1, groups):
    """observed_mix: one hypergeometric over the pooled fixed margins."""
    total = n0 + n1
    m = a0 + a1
    lo, hi = _hg_support(m, n0, total)
    observed = Fraction(a0, n0) - Fraction(a1, n1)
    dist = {}
    for k in range(lo, hi + 1):
        stat = Fraction(k, n0) - Fraction(m - k, n1)
        dist[stat] = dist.get(stat, Fraction(0)) + _hg_prob(m, total, n0, k)
    p, lower, upper = _tails(dist, observed)
    return {'kind': 'pooled_conditional_hypergeometric',
            'null': '固定两组样本量与全体允许数为边际；第一组允许数服从单一超几何分布。',
            'p_definition': P_DEFINITION,
            'tails_definition': TAILS_DEFINITION,
            'margins': {'total': total, 'allowed_total': m,
                        'group_total': {groups[0]: n0, groups[1]: n1}},
            'support': {'lo': lo, 'hi': hi},
            'observed': {'allowed_of_first_group': a0, 'statistic': observed},
            'tails': {'lower': lower, 'upper': upper},
            'p_value': p}


def _stratified_test(strata, cells, groups, weights):
    """common_reference: convolve per-stratum independent hypergeometrics."""
    g0, g1 = groups
    dist = {Fraction(0): Fraction(1)}
    layers = []
    for stratum in strata:
        a0, d0 = cells[(g0, stratum)]
        a1, d1 = cells[(g1, stratum)]
        n0 = a0 + d0
        n1 = a1 + d1
        total = n0 + n1
        m = a0 + a1
        lo, hi = _hg_support(m, n0, total)
        layer = {}
        for k in range(lo, hi + 1):
            stat = weights[stratum] * (Fraction(k, n0) - Fraction(m - k, n1))
            layer[stat] = layer.get(stat, Fraction(0)) + _hg_prob(m, total, n0, k)
        merged = {}
        for base, p0 in dist.items():
            for stat, p1 in layer.items():
                key = base + stat
                merged[key] = merged.get(key, Fraction(0)) + p0 * p1
        dist = merged
        layers.append({'stratum': stratum,
                       'margins': {'total': total, 'allowed_total': m,
                                   'group_total': {g0: n0, g1: n1}},
                       'support': {'lo': lo, 'hi': hi}})
    observed = sum((weights[s] * (Fraction(cells[(g0, s)][0], sum(cells[(g0, s)]))
                                  - Fraction(cells[(g1, s)][0], sum(cells[(g1, s)])))
                    for s in strata), Fraction(0))
    p, lower, upper = _tails(dist, observed)
    return {'kind': 'stratified_independent_hypergeometric_convolution',
            'null': '每层固定两组样本量与该层允许总数；各层第一组允许数服从独立超几何分布，'
                    '加权率差按精确卷积聚合，不先平均各层 p。',
            'p_definition': P_DEFINITION,
            'tails_definition': TAILS_DEFINITION,
            'strata': layers,
            'support': {'lo': min(dist), 'hi': max(dist)},
            'distinct_values': len(dist),
            'observed': {'statistic': observed},
            'tails': {'lower': lower, 'upper': upper},
            'p_value': p}


def _freeze(value):
    if isinstance(value, Fraction):
        return [value.numerator, value.denominator]
    if isinstance(value, dict):
        return {k: _freeze(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_freeze(v) for v in value]
    return value


def _compute(source, contract, batch_id, rule_version, source_digest, contract_digest):
    groups, strata, cells = _matrix(source)
    if not MIN_STRATA <= len(strata) <= MAX_STRATA:
        raise ValueError('group_audit_contract_limits')
    g0, g1 = groups
    size = {}
    for stratum in strata:
        n0 = sum(cells[(g0, stratum)])
        n1 = sum(cells[(g1, stratum)])
        if n0 + n1 > MAX_STRATUM_TOTAL:
            raise ValueError('group_audit_contract_limits')
        size[stratum] = (n0, n1)
    estimand = contract['estimand']
    weights = None
    if estimand == 'common_reference':
        weights = contract['weights']
        if set(weights) != set(strata):
            raise ValueError('group_audit_bad_weights')
        if sum(weights.values(), Fraction(0)) != 1:
            raise ValueError('group_audit_bad_weights')
    table = []
    for stratum in strata:
        row = {'stratum': stratum, 'cells': {}}
        for g in groups:
            allowed, denied = cells[(g, stratum)]
            total = allowed + denied
            row['cells'][g] = {'allowed': allowed, 'denied': denied, 'total': total,
                               'rate': Fraction(allowed, total)}
        table.append(row)
    if estimand == 'observed_mix':
        n_g = {g: sum(size[s][i] for s in strata) for i, g in enumerate(groups)}
        a_g = {g: sum(cells[(g, s)][0] for s in strata) for g in groups}
        rates = {g: Fraction(a_g[g], n_g[g]) for g in groups}
        for row in table:
            stratum = row['stratum']
            row['mix_weight'] = {}
            row['contribution'] = {}
            for i, g in enumerate(groups):
                weight = Fraction(size[stratum][i], n_g[g])
                rate = Fraction(cells[(g, stratum)][0], size[stratum][i])
                row['mix_weight'][g] = weight
                row['contribution'][g] = weight * rate
        test = _pooled_test(a_g[g0], n_g[g0], a_g[g1], n_g[g1], groups)
    else:
        rates = {}
        for i, g in enumerate(groups):
            rates[g] = sum((weights[s] * Fraction(cells[(g, s)][0], size[s][i])
                            for s in strata), Fraction(0))
        for row in table:
            stratum = row['stratum']
            row['weight'] = weights[stratum]
            row['contribution'] = {}
            for i, g in enumerate(groups):
                rate = Fraction(cells[(g, stratum)][0], size[stratum][i])
                row['contribution'][g] = weights[stratum] * rate
        test = _stratified_test(strata, cells, groups, weights)
    gap = rates[g0] - rates[g1]
    abs_gap = abs(gap)
    gap_met = abs_gap >= contract['min_gap']
    p_met = test['p_value'] <= contract['max_p']
    failed = []
    if not gap_met:
        failed.append('min_gap')
    if not p_met:
        failed.append('max_p')
    investigate = gap_met and p_met
    result = {
        'contract_id': contract['contract_id'],
        'audit_id': contract['audit_id'],
        'batch_id': batch_id,
        'rule_version': rule_version,
        'source_digest': source_digest,
        'contract_digest': contract_digest,
        'estimand': estimand,
        'estimand_definition': ESTIMAND_DEFINITIONS[estimand],
        'groups': list(groups),
        'strata': list(strata),
        'thresholds': {'min_gap': contract['min_gap'], 'max_p': contract['max_p']},
        'strata_table': table,
        'group_rates': rates,
        'gap': gap,
        'test': test,
        'trigger': {'abs_gap': abs_gap, 'gap_met': gap_met, 'p_met': p_met,
                    'investigate': investigate, 'failed': failed,
                    'conclusion': '触发调查' if investigate else '未触发调查'},
        'scope_note': SCOPE_NOTE,
    }
    return _freeze(result)


def decide(db_path, contract_path):
    """Freeze a caller-chosen estimation contract and its exact diagnostic."""
    raw = Path(contract_path).read_bytes()
    try:
        doc = json.loads(raw)
    except ValueError:
        raise ValueError('group_audit_bad_contract')
    contract = _parse_contract(doc)
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA + AUDIT_SCHEMA)
        prior = db.execute('SELECT digest FROM group_audit_contract WHERE contract_id=?',
                           (contract['contract_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('group_audit_contract_conflict')
            row = db.execute('SELECT result_json FROM group_audit_diagnostic '
                             'WHERE contract_id=?', (contract['contract_id'],)).fetchone()
            if not row:
                raise ValueError('group_audit_diagnostic_missing')
            return json.loads(row['result_json'])
        source = db.execute('SELECT * FROM group_audit_source WHERE audit_id=?',
                            (contract['audit_id'],)).fetchone()
        if not source:
            raise ValueError('group_audit_unknown_source')
        if hashlib.sha256(source['raw']).hexdigest() != source['digest']:
            raise ValueError('group_audit_source_digest')
        batch = db.execute('SELECT config_raw FROM batch WHERE id=?',
                           (source['batch_id'],)).fetchone()
        if not batch:
            raise ValueError('group_audit_unknown_batch')
        config = json.loads(batch['config_raw'])
        source_doc = json.loads(source['raw'])
        if config['rule_version'] != source_doc['rule_version']:
            raise ValueError('group_audit_wrong_rule_version')
        result = _compute(source_doc, contract, source['batch_id'],
                          source_doc['rule_version'], source['digest'], digest)
        with db:
            db.execute('INSERT INTO group_audit_contract VALUES (?,?,?,?)',
                       (contract['contract_id'], contract['audit_id'], digest, raw))
            db.execute('INSERT INTO group_audit_diagnostic(contract_id,audit_id,batch_id,'
                       'rule_version,source_digest,contract_digest,estimand,result_json) '
                       'VALUES (?,?,?,?,?,?,?,?)',
                       (contract['contract_id'], contract['audit_id'], source['batch_id'],
                        source_doc['rule_version'], source['digest'], digest,
                        contract['estimand'],
                        json.dumps(result, ensure_ascii=False, sort_keys=True)))
        return result
    finally:
        db.close()


def inspect(db_path, contract_id):
    db = connect(db_path)
    try:
        db.executescript(SCHEMA + AUDIT_SCHEMA)
        row = db.execute('SELECT result_json FROM group_audit_diagnostic '
                         'WHERE contract_id=?', (contract_id,)).fetchone()
        if not row:
            raise ValueError('group_audit_unknown_diagnostic')
        return json.loads(row['result_json'])
    finally:
        db.close()


def verify(db_path, contract_id=None):
    """Recompute frozen diagnostics from the frozen source and contract bytes."""
    db = connect(db_path)
    try:
        db.executescript(SCHEMA + AUDIT_SCHEMA)
        if contract_id is None:
            rows = db.execute('SELECT * FROM group_audit_diagnostic '
                              'ORDER BY contract_id').fetchall()
        else:
            rows = db.execute('SELECT * FROM group_audit_diagnostic WHERE contract_id=?',
                              (contract_id,)).fetchall()
            if not rows:
                raise ValueError('group_audit_unknown_diagnostic')
        problems = []
        for diag in rows:
            cid = diag['contract_id']
            contract_row = db.execute('SELECT * FROM group_audit_contract '
                                      'WHERE contract_id=?', (cid,)).fetchone()
            if not contract_row:
                problems.append(f'group_audit_contract_missing:{cid}')
                continue
            if hashlib.sha256(contract_row['raw']).hexdigest() != contract_row['digest']:
                problems.append(f'group_audit_contract_digest:{cid}')
                continue
            source = db.execute('SELECT * FROM group_audit_source WHERE audit_id=?',
                                (diag['audit_id'],)).fetchone()
            if not source:
                problems.append(f'group_audit_source_missing:{cid}')
                continue
            if hashlib.sha256(source['raw']).hexdigest() != source['digest']:
                problems.append(f'group_audit_source_digest:{cid}')
                continue
            if diag['source_digest'] != source['digest'] or diag['batch_id'] != source['batch_id']:
                problems.append(f'group_audit_diagnostic_source:{cid}')
                continue
            if diag['contract_digest'] != contract_row['digest']:
                problems.append(f'group_audit_diagnostic_contract:{cid}')
                continue
            batch = db.execute('SELECT config_raw, config_hash FROM batch WHERE id=?',
                               (diag['batch_id'],)).fetchone()
            if not batch:
                problems.append(f'group_audit_batch_missing:{cid}')
                continue
            if hashlib.sha256(batch['config_raw']).hexdigest() != batch['config_hash']:
                problems.append(f'group_audit_batch_config:{cid}')
                continue
            try:
                config = json.loads(batch['config_raw'])
                source_doc = json.loads(source['raw'])
                if (config['rule_version'] != diag['rule_version']
                        or source_doc['rule_version'] != diag['rule_version']):
                    problems.append(f'group_audit_rule_version:{cid}')
                    continue
                contract = _parse_contract(json.loads(contract_row['raw']))
                expected = _compute(source_doc, contract, diag['batch_id'],
                                    diag['rule_version'], source['digest'],
                                    contract_row['digest'])
                frozen = json.dumps(expected, ensure_ascii=False, sort_keys=True)
                if frozen != diag['result_json']:
                    problems.append(f'group_audit_diagnostic:{cid}')
            except (ValueError, KeyError, TypeError):
                problems.append(f'group_audit_diagnostic:{cid}')
        return problems
    finally:
        db.close()
