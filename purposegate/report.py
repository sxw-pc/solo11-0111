import json
from pathlib import Path
from .store import connect


def export(db_path, out):
    db = connect(db_path); out = Path(out); out.parent.mkdir(parents=True, exist_ok=True)
    run = db.execute('SELECT * FROM run ORDER BY id DESC LIMIT 1').fetchone()
    if not run: raise ValueError('no_run')
    rid = run['id']
    data = {'run': dict(run),
            'sources': [{'id': x['id'], 'name': x['name'], 'digest': x['digest']} for x in db.execute('SELECT * FROM source_file ORDER BY id')],
            'decisions': [dict(x) for x in db.execute('SELECT * FROM decision WHERE run_id=? ORDER BY subject', (rid,))],
            'issues': [dict(x) for x in db.execute('SELECT * FROM issue WHERE run_id=? OR run_id IS NULL ORDER BY id', (rid,))],
            'evidence': [dict(x) for x in db.execute('SELECT * FROM evidence WHERE run_id=? ORDER BY id', (rid,))]}
    data['executions'] = [dict(x) for x in db.execute('SELECT * FROM execution ORDER BY id')]
    data['delegation_chains'] = _chains(db, rid)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    md = out.with_suffix('.md')
    lines = ['# 离线判定报告', '', f'运行 {rid}：{run["status"]}', '',
             '\n'.join(f'- {x["subject"]}: {x["status"]}，{x["detail"]}' for x in data['decisions'])]
    chains = data['delegation_chains']
    if chains:
        lines += ['', '## 委托链证据', '']
        for chain in chains:
            lines.append(f'- {chain["subject"]} → {chain["final_user"]}：{chain["status"]}'
                         f'（合同 {chain["contract"]}，来源 {chain["contract_event_key"] or "缺失"}'
                         f' / {chain["contract_source_sha"] or "缺失"}，效力 {_effect(chain)}）')
            for link in chain['links']:
                lines.append(f'  - L{link["level"]} {link["event_key"]} {link["delegator"]}→{link["delegatee"]}'
                             f' 目的 {link["purpose"]} 字段 {",".join(json.loads(link["fields_json"]))}'
                             f' 交集 {",".join(json.loads(link["field_intersection"]))}'
                             f' 期间 {link["from_at"]}~{link["until_at"]}'
                             f' 来源SHA {link["source_sha"] or "缺失"} 状态 {link["status"]}')
    md.write_text('\n'.join(lines), encoding='utf-8')
    db.close(); return out


def _effect(chain):
    detail = json.loads(chain['detail'])
    return detail.get('revocation_effect', chain['contract'])


def _chains(db, rid):
    chains = []
    for chain in db.execute('SELECT * FROM delegation_chain WHERE run_id=? ORDER BY id', (rid,)):
        item = dict(chain)
        item['links'] = [dict(row) for row in db.execute(
            'SELECT * FROM delegation_link WHERE chain_id=? ORDER BY level', (chain['id'],))]
        chains.append(item)
    return chains


def export_chains(db_path, subject=None, out=None):
    """Print the frozen delegation evidence of the latest run for review."""
    db = connect(db_path)
    run = db.execute('SELECT id FROM run ORDER BY id DESC LIMIT 1').fetchone()
    if not run: raise ValueError('no_run')
    chains = _chains(db, run['id'])
    if subject:
        chains = [chain for chain in chains if chain['subject'] == subject]
    db.close()
    payload = {'run': run['id'], 'subject': subject, 'chains': chains}
    if out:
        target = Path(out); target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        return target
    return payload


def verify(db_path):
    db = connect(db_path); problems = []
    from .ingest import hash_bytes
    for batch in db.execute('SELECT id,config_raw,config_hash FROM batch'):
        if hash_bytes(batch['config_raw']) != batch['config_hash']:
            problems.append(f'batch_config:{batch["id"]}')
    sources = {s['id']: dict(s) for s in db.execute('SELECT * FROM source_file')}
    for s in sources.values():
        if hash_bytes(s['raw']) != s['digest']: problems.append(f'source:{s["id"]}')
    events = {e['id']: dict(e) for e in db.execute('SELECT * FROM raw_event')}
    for e in events.values():
        try:
            lines = sources[e['source_id']]['raw'].decode('utf-8-sig').splitlines()
            original = lines[e['line_no'] - 1]
            if hash_bytes(original.encode('utf-8')) != e['digest'] or json.loads(original) != json.loads(e['payload']):
                problems.append(f'event:{e["id"]}')
        except (KeyError, IndexError, UnicodeError, ValueError):
            problems.append(f'event:{e["id"]}')
    for ev in db.execute('SELECT * FROM evidence WHERE event_id IS NOT NULL'):
        source_event = events.get(ev['event_id'])
        if not source_event or source_event['source_id'] != ev['source_id']:
            problems.append(f'evidence:{ev["id"]}')
    for row in db.execute('SELECT * FROM delegation_link WHERE event_id IS NOT NULL'):
        source_event = events.get(row['event_id'])
        if not source_event or source_event['source_id'] != row['source_id']:
            problems.append(f'delegation_link:{row["id"]}')
        elif row['source_sha'] and source_event['digest'] != row['source_sha']:
            problems.append(f'delegation_source:{row["id"]}')
    for chain in db.execute('SELECT * FROM delegation_chain WHERE contract_event_key IS NOT NULL'):
        source_event = next((e for e in events.values()
                             if json.loads(e['payload']).get('event_key') == chain['contract_event_key']), None)
        if not source_event or source_event['digest'] != chain['contract_source_sha']:
            problems.append(f'delegation_contract:{chain["id"]}')
    for d in db.execute('SELECT * FROM decision'):
        if not db.execute('SELECT 1 FROM evidence WHERE run_id=? AND subject=?', (d['run_id'], d['subject'])).fetchone(): problems.append(f'decision:{d["id"]}')
        if db.execute('SELECT 1 FROM raw_event WHERE subject=?', (d['subject'],)).fetchone() and not db.execute(
                'SELECT 1 FROM evidence WHERE run_id=? AND subject=? AND event_id IS NOT NULL', (d['run_id'], d['subject'])).fetchone():
            problems.append(f'decision_source:{d["id"]}')
    db.close(); return problems
