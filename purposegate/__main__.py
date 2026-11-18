import argparse,tempfile
from pathlib import Path
from .ingest import initialize,load
from .rules import evaluate, action
from .report import export,verify
def main():
    p=argparse.ArgumentParser(description='离线证据判定工具');sub=p.add_subparsers(dest='command',required=True)
    d=sub.add_parser('demo');d.add_argument('--workdir')
    i=sub.add_parser('init');i.add_argument('--db',required=True);i.add_argument('--config',required=True)
    ga=sub.add_parser('group-audit-record');ga.add_argument('--db',required=True);ga.add_argument('--input',required=True)
    gr=sub.add_parser('group-audit-raw');gr.add_argument('--db',required=True);gr.add_argument('--id',required=True)
    gd=sub.add_parser('group-audit-decide');gd.add_argument('--db',required=True);gd.add_argument('--contract',required=True)
    gv=sub.add_parser('group-audit-verify');gv.add_argument('--db',required=True);gv.add_argument('--id')
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
        from . import group_audit
        gdb=work/'group-audit.db'
        initialize(gdb,fixture/'group-audit-config.json')
        group_audit.record(gdb,fixture/'group-audit-matrix.json')
        print(group_audit.raw_summary(gdb,'audit-S1'))
        print(group_audit.decide(gdb,fixture/'group-audit-contract-observed.json')['trigger'])
        print(group_audit.decide(gdb,fixture/'group-audit-contract-common.json')['trigger'])
        print(group_audit.verify(gdb))
    elif a.command=='init':print(initialize(a.db,a.config))
    elif a.command=='group-audit-record':
        from .group_audit import record
        print(record(a.db,a.input))
    elif a.command=='group-audit-raw':
        from .group_audit import raw_summary
        print(raw_summary(a.db,a.id))
    elif a.command=='group-audit-decide':
        from .group_audit import decide
        print(decide(a.db,a.contract))
    elif a.command=='group-audit-verify':
        from . import group_audit
        print(group_audit.verify(a.db,a.id))
    elif a.command=='load':print(load(a.db,a.path))
    elif a.command=='evaluate':print(evaluate(a.db))
    elif a.command=='report':print(export(a.db,a.out))
    elif a.command=='verify':
        from . import rules
        checks=verify(a.db)
        if hasattr(rules,'verify_decisions'):checks+=rules.verify_decisions(a.db)
        target=rules.verify_target(a.db,a.sandbox) if a.sandbox and hasattr(rules,'verify_target') else []
        if a.out:
            import json
            out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True)
            out.write_text(json.dumps({'integrity_problems':checks,'target':target},ensure_ascii=False,indent=2),encoding='utf-8')
            out.with_suffix('.md').write_text('# 独立对账\n\n'+'\n'.join(f'- {x}' for x in target),encoding='utf-8')
        print({'integrity_problems':checks,'target':target})
    elif a.command=='delivery-prepare':
        import json
        from .delivery import prepare
        print(prepare(a.db,a.id,a.request,json.loads(Path(a.values).read_text(encoding='utf-8'))))
    elif a.command=='delivery-release':
        import json
        from .delivery import release
        print(release(a.db,a.id,json.loads(Path(a.values).read_text(encoding='utf-8')),a.out))
    elif a.command=='usage-record':
        import json
        from .usage import record
        rows=[json.loads(line) for line in Path(a.events).read_text(encoding='utf-8').splitlines() if line.strip()]
        print(record(a.db,rows))
    elif a.command=='usage-audit':
        from .usage import audit
        print(audit(a.db,a.id))
    else:print(action(a.command,a.db,sandbox=getattr(a,'sandbox',None),out=getattr(a,'out',None)))
if __name__=='__main__':main()
