"""合成双提供方候选链接：清单接收、人工映射评估与规范实体配对证书。

record 冻结左右不透明记录 ID 清单、候选二部边和每边至少两份不同来源的
独立评分声明；evaluate_manual 只评价调用者给出的一对一完整映射，不寻找
最优配对；decide 按治理者事先选定的复核合同在冻结候选图上精确求解完整
配对并签发证书；verify_certificate 从冻结原始清单独立重算全部结论。

配对结果仅是合成记录的实际对应关系：不创建联合数据产品，不构成任何人
的授权，也不接入 rules.py 授权判定、delivery.py 交付或 usage.py 用后审计。
"""

import hashlib
import itertools
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
CREATE TABLE IF NOT EXISTS link_certificate (
 certificate_id TEXT PRIMARY KEY, manifest_id TEXT NOT NULL REFERENCES link_manifest(manifest_id),
 contract TEXT NOT NULL, digest TEXT NOT NULL, raw BLOB NOT NULL, result TEXT NOT NULL);
"""

MAX_RECORDS_PER_SIDE = 12
MAX_EDGES = 80
DEFAULT_BUDGET = 10_000_000
CONTRACTS = ('max_total_score', 'max_min_pair_score')
DISCLAIMER = ('规范实体键仅由冻结清单与配对身份派生，是本地合成 ID；'
              '不宣称链接对真实人员绝对正确，不构成任何人的授权，不创建联合数据产品。')


def _validate_manifest_doc(doc):
    left = doc['left_ids']
    right = doc['right_ids']
    if not doc['manifest_id'] or not left or len(left) != len(right):
        raise ValueError('link_bad_manifest')
    if len(left) > MAX_RECORDS_PER_SIDE or len(right) > MAX_RECORDS_PER_SIDE or (
            len(doc['edges']) > MAX_EDGES):
        raise ValueError('link_manifest_too_large')
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


def _check_batch(db, doc):
    batch = db.execute('SELECT config_raw FROM batch WHERE id=?', (doc['batch_id'],)).fetchone()
    if not batch:
        raise ValueError('link_unknown_batch')
    dataset = json.loads(batch['config_raw'])['dataset']
    if dataset['version'] != doc['dataset_version']:
        raise ValueError('link_dataset_mismatch')
    return dataset['version']


def record(db_path, input_path):
    raw = Path(input_path).read_bytes()
    doc = json.loads(raw)
    _validate_manifest_doc(doc)
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        _check_batch(db, doc)
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


def _edge_map(doc):
    """候选二部图：每边保守分取其全部独立声明的最小分值，声明按 ID 排序固定。"""
    edges = {}
    for edge in doc['edges']:
        claims = sorted(({'id': c['id'], 'source': c['source'], 'score': c['score']}
                         for c in edge['claims']), key=lambda c: c['id'])
        edges[(edge['left'], edge['right'])] = {
            'claims': claims, 'score': min(c['score'] for c in claims)}
    return edges


def _entity_key(manifest_digest, left, right):
    # 规范实体键只由冻结清单摘要与配对身份派生，是本地合成 ID。
    token = f'{manifest_digest}\n{left}\n{right}'.encode()
    return 'ent-' + hashlib.sha256(token).hexdigest()[:24]


def _objective(value, contract):
    total, minimum, _tie = value
    return (total, minimum) if contract == 'max_total_score' else (minimum, total)


def _better(candidate, incumbent, contract):
    """合同字典序比较：先按合同排目标，仍并列按冻结 left/right ID 序破同分。"""
    if incumbent is None:
        return True
    if _objective(candidate, contract) != _objective(incumbent, contract):
        return _objective(candidate, contract) > _objective(incumbent, contract)
    return candidate[2] < incumbent[2]


def _solve(doc, contract, budget):
    """有限位掩码 DP 精确求解完整一对一配对；预算耗尽时报 indeterminate。"""
    left_ids = list(doc['left_ids'])
    right_ids = list(doc['right_ids'])
    edges = _edge_map(doc)
    left_index = {lid: i for i, lid in enumerate(left_ids)}
    right_index = {rid: j for j, rid in enumerate(right_ids)}
    by_left = [[] for _ in left_ids]
    for (left, right), info in edges.items():
        by_left[left_index[left]].append((right_index[right], info['score']))
    n = len(left_ids)
    full = (1 << n) - 1
    states = {0: (0, None, ())}
    relaxations = 0
    dp_states = 0
    exhausted = False
    for i in range(n):
        nxt = {}
        for mask in sorted(states):
            value = states[mask]
            dp_states += 1
            for j, score in by_left[i]:
                if mask >> j & 1:
                    continue
                relaxations += 1
                if relaxations > budget:
                    exhausted = True
                    break
                candidate = (value[0] + score,
                             score if value[1] is None else min(value[1], score),
                             value[2] + (j,))
                key = mask | (1 << j)
                if _better(candidate, nxt.get(key), contract):
                    nxt[key] = candidate
            if exhausted:
                break
        if exhausted:
            break
        states = nxt
        if not states:
            break
    base = {'relaxations': relaxations, 'dp_states': dp_states}
    if exhausted:
        bounds, witness = _bounds_and_witness(doc, edges)
        return {'status': 'indeterminate', 'bounds': bounds, 'witness': witness, **base}
    if states and full in states:
        total, minimum, assignment = states[full]
        return {'status': 'optimal', 'assignment': assignment,
                'total': total, 'minimum': minimum, **base}
    return {'status': 'no_perfect_matching',
            'blocking_cut': _blocking_cut(doc, edges), **base}


def _bounds_and_witness(doc, edges):
    """indeterminate 时的已证上下界；下界来自一个真实可行的贪心映射，不冒称最优。"""
    left_ids = list(doc['left_ids'])
    right_ids = list(doc['right_ids'])
    left_index = {lid: i for i, lid in enumerate(left_ids)}
    right_index = {rid: j for j, rid in enumerate(right_ids)}
    max_per_left = [0] * len(left_ids)
    for (left, _right), info in edges.items():
        i = left_index[left]
        max_per_left[i] = max(max_per_left[i], info['score'])
    ordered = sorted(edges.items(),
                     key=lambda kv: (-kv[1]['score'], left_index[kv[0][0]], right_index[kv[0][1]]))
    used_left = set()
    used_right = set()
    pairs = []
    for (left, right), info in ordered:
        if left in used_left or right in used_right:
            continue
        used_left.add(left)
        used_right.add(right)
        pairs.append({'left': left, 'right': right, 'conservative_score': info['score']})
    witness = None
    if len(pairs) == len(left_ids):
        pairs.sort(key=lambda p: left_index[p['left']])
        witness = {'pairs': pairs,
                   'total_score': sum(p['conservative_score'] for p in pairs),
                   'minimum_score': min(p['conservative_score'] for p in pairs),
                   'optimal': False}
    bounds = {'total_score': {'lower': witness['total_score'] if witness else None,
                              'upper': sum(max_per_left)},
              'minimum_score': {'lower': witness['minimum_score'] if witness else None,
                                'upper': min(max_per_left)}}
    return bounds, witness


def _blocking_cut(doc, edges):
    """最小霍尔违例（不可匹配集合）：|N(S)| < |S|，可独立复核，不凭空补边。"""
    left_ids = list(doc['left_ids'])
    right_ids = list(doc['right_ids'])
    neighbors = {lid: set() for lid in left_ids}
    for left, right in edges:
        neighbors[left].add(right)
    for size in range(1, len(left_ids) + 1):
        for combo in itertools.combinations(range(len(left_ids)), size):
            group = [left_ids[i] for i in combo]
            reach = set().union(*(neighbors[lid] for lid in group))
            if len(reach) < len(group):
                return {'left_set': group,
                        'neighborhood': [rid for rid in right_ids if rid in reach],
                        'deficiency': len(group) - len(reach),
                        'proof': 'hall_violator'}
    return None


def _certificate(doc, manifest_digest, dataset_version, plan, solution, budget):
    left_ids = list(doc['left_ids'])
    right_ids = list(doc['right_ids'])
    edges = _edge_map(doc)
    left_index = {lid: i for i, lid in enumerate(left_ids)}
    right_index = {rid: j for j, rid in enumerate(right_ids)}
    candidate_edges = [
        {'left': left, 'right': right, 'claims': info['claims'],
         'conservative_score': info['score']}
        for (left, right), info in sorted(
            edges.items(), key=lambda kv: (left_index[kv[0][0]], right_index[kv[0][1]]))]
    cert = {'certificate_id': plan['certificate_id'],
            'manifest_id': doc['manifest_id'],
            'manifest_digest': manifest_digest,
            'batch_id': doc['batch_id'],
            'dataset_version': dataset_version,
            'left_provider': doc.get('left_provider'),
            'right_provider': doc.get('right_provider'),
            'left_ids': left_ids,
            'right_ids': right_ids,
            'candidate_edges': candidate_edges,
            'contract': plan['contract'],
            'status': solution['status'],
            'entity_key_scope': 'local_synthetic',
            'disclaimer': DISCLAIMER,
            'evidence': {'budget': budget,
                         'dp_states': solution['dp_states'],
                         'relaxations': solution['relaxations'],
                         'exhaustive': solution['status'] != 'indeterminate'}}
    if solution['status'] == 'optimal':
        entities = []
        for i, j in enumerate(solution['assignment']):
            left, right = left_ids[i], right_ids[j]
            info = edges[(left, right)]
            entities.append({'entity_key': _entity_key(manifest_digest, left, right),
                             'left': left, 'right': right,
                             'claims': info['claims'],
                             'conservative_score': info['score']})
        summary = {}
        for entity in entities:
            for claim in entity['claims']:
                summary[claim['source']] = summary.get(claim['source'], 0) + 1
        cert['canonical_entities'] = entities
        cert['total_score'] = solution['total']
        cert['minimum_score'] = solution['minimum']
        cert['source_summary'] = dict(sorted(summary.items()))
        cert['optimality'] = {
            'contract': plan['contract'],
            'ranked_objective': ([solution['total'], solution['minimum']]
                                 if plan['contract'] == 'max_total_score'
                                 else [solution['minimum'], solution['total']]),
            'tie_break': 'frozen_left_right_id_order',
            'proved_by': 'exact_bitmask_dp'}
    elif solution['status'] == 'no_perfect_matching':
        cert['canonical_entities'] = []
        cert['blocking_cut'] = solution['blocking_cut']
    else:
        cert['canonical_entities'] = []
        cert['bounds'] = solution['bounds']
        if solution['witness']:
            cert['feasible_witness'] = solution['witness']
    return cert


def decide(db_path, input_path):
    """按事先选定的复核合同签发规范实体配对证书；相同计划重放幂等。"""
    raw = Path(input_path).read_bytes()
    plan = json.loads(raw)
    contract = plan.get('contract')
    if not plan.get('certificate_id') or contract not in CONTRACTS:
        raise ValueError('link_bad_plan')
    budget = plan.get('budget', DEFAULT_BUDGET)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise ValueError('link_bad_budget')
    digest = hashlib.sha256(raw).hexdigest()
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        prior = db.execute('SELECT digest,result FROM link_certificate WHERE certificate_id=?',
                           (plan['certificate_id'],)).fetchone()
        if prior:
            if prior['digest'] != digest:
                raise ValueError('link_certificate_conflict')
            return json.loads(prior['result'])
        row = db.execute('SELECT raw,digest FROM link_manifest WHERE manifest_id=?',
                         (plan['manifest_id'],)).fetchone()
        if not row:
            raise ValueError('link_unknown_manifest')
        doc = json.loads(row['raw'])
        _validate_manifest_doc(doc)
        dataset_version = _check_batch(db, doc)
        solution = _solve(doc, contract, budget)
        cert = _certificate(doc, row['digest'], dataset_version, plan, solution, budget)
        with db:
            db.execute('INSERT INTO link_certificate VALUES (?,?,?,?,?,?)',
                       (plan['certificate_id'], plan['manifest_id'], contract, digest, raw,
                        json.dumps(cert, sort_keys=True)))
        return cert
    finally:
        db.close()


def _check_certificate_body(doc, cert, expected):
    problems = []
    edges = _edge_map(doc)
    if cert.get('candidate_edges') != expected['candidate_edges']:
        problems.append('candidate_graph_mismatch')
    status = cert.get('status')
    if status != expected['status']:
        problems.append('status_mismatch')
    if status == 'optimal':
        entities = cert.get('canonical_entities') or []
        lefts = [e.get('left') for e in entities]
        rights = [e.get('right') for e in entities]
        if len(set(lefts)) != len(lefts):
            problems.append('duplicate_left')
        if len(set(rights)) != len(rights):
            problems.append('duplicate_right')
        if sorted(map(str, lefts)) != sorted(doc['left_ids']) or (
                sorted(map(str, rights)) != sorted(doc['right_ids'])):
            problems.append('not_one_to_one_complete')
        for entity in entities:
            info = edges.get((entity.get('left'), entity.get('right')))
            if info is None:
                problems.append('unsupported_pair')
                continue
            if entity.get('claims') != info['claims']:
                problems.append('forged_claim')
            if entity.get('conservative_score') != info['score']:
                problems.append('bad_conservative_score')
            if entity.get('entity_key') != _entity_key(cert.get('manifest_digest'),
                                                       entity.get('left'), entity.get('right')):
                problems.append('bad_entity_key')
        scores = [e.get('conservative_score') for e in entities]
        if len(entities) == len(doc['left_ids']) and all(
                isinstance(s, int) and not isinstance(s, bool) for s in scores):
            if cert.get('total_score') != sum(scores):
                problems.append('total_mismatch')
            if cert.get('minimum_score') != min(scores):
                problems.append('minimum_mismatch')
        if expected['status'] == 'optimal':
            contract = expected['contract']
            have = (cert.get('total_score'), cert.get('minimum_score'))
            if all(isinstance(x, int) and not isinstance(x, bool) for x in have):
                want = (expected['total_score'], expected['minimum_score'])
                if contract != 'max_total_score':
                    have, want = (have[1], have[0]), (want[1], want[0])
                if have < want:
                    problems.append('suboptimal_mapping')
            cert_pairs = sorted((str(e.get('left')), str(e.get('right'))) for e in entities)
            want_pairs = sorted((e['left'], e['right']) for e in expected['canonical_entities'])
            if cert_pairs != want_pairs:
                problems.append('pairing_mismatch')
    elif status == 'no_perfect_matching':
        cut = cert.get('blocking_cut')
        valid = isinstance(cut, dict)
        if valid:
            left_set = set(cut.get('left_set') or [])
            neighborhood = set(cut.get('neighborhood') or [])
            actual = {r for (l, r) in edges if l in left_set}
            valid = (bool(left_set) and left_set <= set(doc['left_ids'])
                     and actual == neighborhood and len(neighborhood) < len(left_set)
                     and cut.get('deficiency') == len(left_set) - len(neighborhood))
        if not valid:
            problems.append('bad_blocking_cut')
        elif expected['status'] == 'no_perfect_matching' and cut != expected['blocking_cut']:
            problems.append('bad_blocking_cut')
    elif status == 'indeterminate':
        bounds = cert.get('bounds') or {}
        for key in ('total_score', 'minimum_score'):
            entry = bounds.get(key) or {}
            lo, hi = entry.get('lower'), entry.get('upper')
            if lo is not None and hi is not None and lo > hi:
                problems.append('bad_bounds')
        witness = cert.get('feasible_witness')
        if witness is not None:
            wpairs = [(p.get('left'), p.get('right')) for p in witness.get('pairs') or []]
            if (any(pair not in edges for pair in wpairs)
                    or len({l for l, _ in wpairs}) != len(doc['left_ids'])
                    or len({r for _, r in wpairs}) != len(doc['right_ids'])):
                problems.append('bad_witness')
    return problems


def verify_certificate(db_path, certificate_id):
    """从冻结原始 manifest 独立重算候选图、保守分、完整匹配与目标最优性。"""
    db = connect(db_path)
    try:
        db.executescript(SCHEMA)
        row = db.execute('SELECT * FROM link_certificate WHERE certificate_id=?',
                         (certificate_id,)).fetchone()
        if not row:
            raise ValueError('link_unknown_certificate')
        cert = json.loads(row['result'])
        problems = []
        checks = []
        if hashlib.sha256(row['raw']).hexdigest() != row['digest']:
            problems.append('plan_digest_mismatch')
        try:
            plan = json.loads(row['raw'])
        except ValueError:
            return {'certificate_id': certificate_id, 'ok': False,
                    'problems': sorted(set(problems + ['plan_unparseable'])), 'checks': checks}
        mrow = db.execute('SELECT raw,digest FROM link_manifest WHERE manifest_id=?',
                          (row['manifest_id'],)).fetchone()
        if not mrow:
            return {'certificate_id': certificate_id, 'ok': False,
                    'problems': sorted(set(problems + ['unknown_manifest'])), 'checks': checks}
        manifest_digest = hashlib.sha256(mrow['raw']).hexdigest()
        if manifest_digest != mrow['digest']:
            problems.append('manifest_digest_mismatch')
        try:
            doc = json.loads(mrow['raw'])
        except ValueError:
            return {'certificate_id': certificate_id, 'ok': False,
                    'problems': sorted(set(problems + ['manifest_unparseable'])), 'checks': checks}
        manifest_ok = True
        try:
            _validate_manifest_doc(doc)
            checks.append('manifest_structure')
        except ValueError as exc:
            manifest_ok = False
            problems.append(f'invalid_manifest:{exc}')
        try:
            dataset_version = _check_batch(db, doc)
            checks.append('dataset_version')
        except ValueError as exc:
            dataset_version = None
            problems.append(f'invalid_manifest:{exc}')
        contract = plan.get('contract')
        budget = plan.get('budget', DEFAULT_BUDGET)
        budget_ok = isinstance(budget, int) and not isinstance(budget, bool) and budget >= 1
        if contract not in CONTRACTS:
            problems.append('bad_plan_contract')
        if not budget_ok:
            problems.append('bad_plan_budget')
        for field, expected_value in (('manifest_id', doc.get('manifest_id')),
                                      ('manifest_digest', manifest_digest),
                                      ('batch_id', doc.get('batch_id')),
                                      ('dataset_version', dataset_version),
                                      ('left_provider', doc.get('left_provider')),
                                      ('right_provider', doc.get('right_provider')),
                                      ('left_ids', doc.get('left_ids')),
                                      ('right_ids', doc.get('right_ids')),
                                      ('contract', contract)):
            if cert.get(field) != expected_value:
                problems.append(f'frozen_field_mismatch:{field}')
        checks.append('frozen_fields')
        if manifest_ok and dataset_version is not None and contract in CONTRACTS and budget_ok:
            solution = _solve(doc, contract, budget)
            expected = _certificate(doc, manifest_digest, dataset_version, plan, solution, budget)
            checks += ['candidate_graph', 'conservative_scores', 'objective_optimality']
            problems += _check_certificate_body(doc, cert, expected)
            if cert != expected:
                problems.append('result_mismatch')
        return {'certificate_id': certificate_id, 'ok': not problems,
                'problems': sorted(set(problems)), 'checks': checks}
    finally:
        db.close()
