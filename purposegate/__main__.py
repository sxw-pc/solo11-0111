import argparse,json,tempfile
from pathlib import Path
from .ingest import initialize,load
from .rules import evaluate, action
from .report import export,verify
def main():
    p=argparse.ArgumentParser(description='离线证据判定工具');sub=p.add_subparsers(dest='command',required=True)
    d=sub.add_parser('demo');d.add_argument('--workdir')
    i=sub.add_parser('init');i.add_argument('--db',required=True);i.add_argument('--config',required=True)
    ar=sub.add_parser('anon-record');ar.add_argument('--db',required=True);ar.add_argument('--input',required=True)
    am=sub.add_parser('anon-manual');am.add_argument('--db',required=True);am.add_argument('--input',required=True)
    ao=sub.add_parser('anon-optimize');ao.add_argument('--db',required=True);ao.add_argument('--input',required=True)
    av=sub.add_parser('anon-verify');av.add_argument('--db',required=True);av.add_argument('--id',required=True)
    l=sub.add_parser('load');l.add_argument('--db',required=True);l.add_argument('path')
    e=sub.add_parser('evaluate');e.add_argument('--db',required=True)
    r=sub.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    v=sub.add_parser('verify');v.add_argument('--db',required=True);v.add_argument('--sandbox');v.add_argument('--out')
    for name in ('plan','recheck'):
        x=sub.add_parser(name);x.add_argument('--db',required=True);x.add_argument('--sandbox');x.add_argument('--out')
    dp=sub.add_parser('delivery-prepare');dp.add_argument('--db',required=True);dp.add_argument('--id',required=True);dp.add_argument('--request',required=True);dp.add_argument('--values',required=True)
    dr=sub.add_parser('delivery-release');dr.add_argument('--db',required=True);dr.add_argument('--id',required=True);dr.add_argument('--values',required=True);dr.add_argument('--out',required=True)
    ur=sub.add_parser('usage-record');ur.add_argument('--db',required=True);ur.add_argument('--events',required=True)
    ua=sub.add_parser('usage-audit');ua.add_argument('--db',required=True);ua.add_argument('--id',required=True)
    a=p.parse_args()
    if a.command=='demo':
        work=Path(a.workdir or tempfile.mkdtemp(prefix='evidence-'));work.mkdir(parents=True,exist_ok=True);db=work/'ledger.db';fixture=Path(__file__).resolve().parent.parent/'fixtures'
        initialize(db,fixture/'config.json');load(db,fixture/'events.jsonl');print(evaluate(db));print(export(db,work/'report.json'))
        from .anonymize import optimize, record, verify_optimization
        initialize(db,fixture/'anon-config.json');record(db,fixture/'anon-source.json')
        for name in ('anon-opt-l1.json','anon-opt-l2.json'):
            request=json.loads((fixture/name).read_text(encoding='utf-8'))
            result=optimize(db,fixture/name)
            print({'plan_id':result['plan_id'],'status':result['status'],'loss':result['loss'],
                   'classes':[(eq['qi'],eq['size'],eq['sensitive_distinct'])
                              for eq in result['equivalence_classes']]})
            print(verify_optimization(db,request['plan_id']))
    elif a.command=='init':print(initialize(a.db,a.config))
    elif a.command=='anon-record':
        from .anonymize import record
        print(record(a.db,a.input))
    elif a.command=='anon-manual':
        from .anonymize import evaluate_manual
        print(evaluate_manual(a.db,a.input))
    elif a.command=='anon-optimize':
        from .anonymize import optimize
        print(json.dumps(optimize(a.db,a.input),ensure_ascii=False,sort_keys=True))
    elif a.command=='anon-verify':
        from .anonymize import verify_optimization
        print(json.dumps(verify_optimization(a.db,a.id),ensure_ascii=False,sort_keys=True))
    elif a.command=='load':print(load(a.db,a.path))
    elif a.command=='evaluate':print(evaluate(a.db))
    elif a.command=='report':print(export(a.db,a.out))
    elif a.command=='verify':
        from . import rules
        checks=verify(a.db)
        if hasattr(rules,'verify_decisions'):checks+=rules.verify_decisions(a.db)
        target=rules.verify_target(a.db,a.sandbox) if a.sandbox and hasattr(rules,'verify_target') else []
        if a.out:
            out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True)
            out.write_text(json.dumps({'integrity_problems':checks,'target':target},ensure_ascii=False,indent=2),encoding='utf-8')
            out.with_suffix('.md').write_text('# 独立对账\n\n'+'\n'.join(f'- {x}' for x in target),encoding='utf-8')
        print({'integrity_problems':checks,'target':target})
    elif a.command=='delivery-prepare':
        from .delivery import prepare
        print(prepare(a.db,a.id,a.request,json.loads(Path(a.values).read_text(encoding='utf-8'))))
    elif a.command=='delivery-release':
        from .delivery import release
        print(release(a.db,a.id,json.loads(Path(a.values).read_text(encoding='utf-8')),a.out))
    elif a.command=='usage-record':
        from .usage import record
        rows=[json.loads(line) for line in Path(a.events).read_text(encoding='utf-8').splitlines() if line.strip()]
        print(record(a.db,rows))
    elif a.command=='usage-audit':
        from .usage import audit
        print(audit(a.db,a.id))
    else:print(action(a.command,a.db,sandbox=getattr(a,'sandbox',None),out=getattr(a,'out',None)))
if __name__=='__main__':main()
