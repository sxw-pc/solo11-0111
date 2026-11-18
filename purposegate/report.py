import json
from pathlib import Path
from .store import connect
def export(db_path,out):
    db=connect(db_path);out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
    run=db.execute('SELECT * FROM run ORDER BY id DESC LIMIT 1').fetchone()
    if not run:raise ValueError('no_run')
    rid=run['id'];data={'run':dict(run),'sources':[{'id':x['id'],'name':x['name'],'digest':x['digest']} for x in db.execute('SELECT * FROM source_file ORDER BY id')],
      'decisions':[dict(x) for x in db.execute('SELECT * FROM decision WHERE run_id=? ORDER BY subject',(rid,))],
      'issues':[dict(x) for x in db.execute('SELECT * FROM issue WHERE run_id=? OR run_id IS NULL ORDER BY id',(rid,))],
      'evidence':[dict(x) for x in db.execute('SELECT * FROM evidence WHERE run_id=? ORDER BY id',(rid,))]}
    data['executions']=[dict(x) for x in db.execute('SELECT * FROM execution ORDER BY id')]
    out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    md=out.with_suffix('.md');md.write_text('# 离线判定报告\n\n'+f'运行 {rid}：{run["status"]}\n\n'+'\n'.join(f'- {x["subject"]}: {x["status"]}，{x["detail"]}' for x in data['decisions']),encoding='utf-8')
    db.close();return out
def verify(db_path):
    db=connect(db_path);problems=[]
    from .ingest import hash_bytes
    for batch in db.execute('SELECT id,config_raw,config_hash FROM batch'):
        if hash_bytes(batch['config_raw'])!=batch['config_hash']:
            problems.append(f'batch_config:{batch["id"]}')
    sources={s['id']:dict(s) for s in db.execute('SELECT * FROM source_file')}
    for s in sources.values():
        from .ingest import hash_bytes
        if hash_bytes(s['raw'])!=s['digest']:problems.append(f'source:{s["id"]}')
    events={e['id']:dict(e) for e in db.execute('SELECT * FROM raw_event')}
    for e in events.values():
        try:
            lines=sources[e['source_id']]['raw'].decode('utf-8-sig').splitlines()
            original=lines[e['line_no']-1]
            if hash_bytes(original.encode('utf-8'))!=e['digest'] or json.loads(original)!=json.loads(e['payload']):
                problems.append(f'event:{e["id"]}')
        except (KeyError,IndexError,UnicodeError,ValueError):
            problems.append(f'event:{e["id"]}')
    for ev in db.execute('SELECT * FROM evidence WHERE event_id IS NOT NULL'):
        source_event=events.get(ev['event_id'])
        if not source_event or source_event['source_id']!=ev['source_id']:
            problems.append(f'evidence:{ev["id"]}')
    for d in db.execute('SELECT * FROM decision'):
        if not db.execute('SELECT 1 FROM evidence WHERE run_id=? AND subject=?',(d['run_id'],d['subject'])).fetchone():problems.append(f'decision:{d["id"]}')
        if db.execute('SELECT 1 FROM raw_event WHERE subject=?',(d['subject'],)).fetchone() and not db.execute(
            'SELECT 1 FROM evidence WHERE run_id=? AND subject=? AND event_id IS NOT NULL',(d['run_id'],d['subject'])).fetchone():
            problems.append(f'decision_source:{d["id"]}')
    db.close();return problems


def poll_export(db_path,out):
    """Snapshot the frozen polling ledger: forecast/plan identity, digests and
    optimization status.  Raw bytes never leave the store in this report."""
    from .polling import SCHEMA as POLL_SCHEMA
    db=connect(db_path)
    db.executescript(POLL_SCHEMA)
    data={'forecasts':[dict(r) for r in db.execute(
            'SELECT forecast_id,batch_id,digest FROM poll_forecast ORDER BY forecast_id')],
      'manual_plans':[dict(r) for r in db.execute(
            'SELECT plan_id,forecast_id,digest,result FROM poll_manual_plan ORDER BY plan_id')],
      'optimized_plans':[dict(r) for r in db.execute(
            'SELECT plan_id,forecast_id,contract,digest,result FROM poll_opt_plan ORDER BY plan_id')]}
    for group in ('manual_plans','optimized_plans'):
        for row in data[group]:
            row['result']=json.loads(row['result'])
    out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 离线轮询账本','',f'- 冻结预测 {len(data["forecasts"])} 份',
           f'- 人工计划 {len(data["manual_plans"])} 份',
           f'- 最优计划 {len(data["optimized_plans"])} 份','']
    for row in data['optimized_plans']:
        result=row['result']
        lines.append(f'- {row["plan_id"]}（{row["contract"]}）：{result.get("status")}，'
                     f'暴露 {result.get("expected_stale_num","-")}/{result.get("expected_stale_den","-")}，'
                     f'最长间隔 {result.get("max_gap","-")}，digest {row["digest"][:12]}…')
    out.with_suffix('.md').write_text('\n'.join(lines),encoding='utf-8')
    db.close();return out

