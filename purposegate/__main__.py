import argparse
import json
from pathlib import Path

from .ingest import initialize, load
from .rules import evaluate, action
from .report import export, verify
from .demo import run_demo


def _print(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main():
    p = argparse.ArgumentParser(description='离线证据判定工具')
    sub = p.add_subparsers(dest='command', required=True)
    d = sub.add_parser('demo')
    d.add_argument('--workdir')
    i = sub.add_parser('init')
    i.add_argument('--db', required=True)
    i.add_argument('--config', required=True)
    l = sub.add_parser('load')
    l.add_argument('--db', required=True)
    l.add_argument('path')
    cs = sub.add_parser('cohort-snapshot')
    cs.add_argument('--db', required=True)
    cs.add_argument('path')
    cc = sub.add_parser('cohort-corrections')
    cc.add_argument('--db', required=True)
    cc.add_argument('path')
    cp = sub.add_parser('correction-plan')
    cp.add_argument('--db', required=True)
    cp.add_argument('--contract', required=True,
                    choices=['retroactive_restate', 'prospective_version'])
    cp.add_argument('--run', dest='run_id', type=int)
    ca = sub.add_parser('correction-apply')
    ca.add_argument('--db', required=True)
    ca.add_argument('--plan')
    cv = sub.add_parser('correction-verify')
    cv.add_argument('--db', required=True)
    cv.add_argument('--plan')
    cv.add_argument('--out')
    cq = sub.add_parser('cohort-query')
    cq.add_argument('--db', required=True)
    cq.add_argument('--cohort', required=True)
    cq.add_argument('--person', required=True)
    cq.add_argument('--metric', required=True, choices=['mean', 'cohort_rank'])
    cq.add_argument('--at', required=True)
    cq.add_argument('--contract')
    e = sub.add_parser('evaluate')
    e.add_argument('--db', required=True)
    r = sub.add_parser('report')
    r.add_argument('--db', required=True)
    r.add_argument('--out', required=True)
    v = sub.add_parser('verify')
    v.add_argument('--db', required=True)
    v.add_argument('--sandbox')
    v.add_argument('--out')
    for name in ('plan', 'recheck'):
        x = sub.add_parser(name)
        x.add_argument('--db', required=True)
        x.add_argument('--sandbox')
        x.add_argument('--out')
    dp = sub.add_parser('delivery-prepare')
    dp.add_argument('--db', required=True)
    dp.add_argument('--id', required=True)
    dp.add_argument('--request', required=True)
    dp.add_argument('--values', required=True)
    dr = sub.add_parser('delivery-release')
    dr.add_argument('--db', required=True)
    dr.add_argument('--id', required=True)
    dr.add_argument('--values', required=True)
    dr.add_argument('--out', required=True)
    ur = sub.add_parser('usage-record')
    ur.add_argument('--db', required=True)
    ur.add_argument('--events', required=True)
    ua = sub.add_parser('usage-audit')
    ua.add_argument('--db', required=True)
    ua.add_argument('--id', required=True)
    a = p.parse_args()
    if a.command == 'demo':
        _print(run_demo(Path(a.workdir) if a.workdir else None))
    elif a.command == 'init':
        print(initialize(a.db, a.config))
    elif a.command == 'load':
        print(load(a.db, a.path))
    elif a.command == 'cohort-snapshot':
        from .cohort import record_snapshot
        print(record_snapshot(a.db, a.path))
    elif a.command == 'cohort-corrections':
        from .cohort import record_corrections
        print(record_corrections(a.db, a.path))
    elif a.command == 'correction-plan':
        from .cohort import build_plan
        _print(build_plan(a.db, a.contract, a.run_id))
    elif a.command == 'correction-apply':
        from .cohort import apply_plan
        _print(apply_plan(a.db, a.plan))
    elif a.command == 'correction-verify':
        from .cohort import plan_issues
        checks = verify(a.db)
        issues = plan_issues(a.db, a.plan) if a.plan else []
        payload = {'integrity_problems': checks, 'plan_issues': issues}
        if a.out:
            out = Path(a.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding='utf-8')
        _print(payload)
    elif a.command == 'cohort-query':
        from .cohort import query_metric
        _print(query_metric(a.db, a.cohort, a.person, a.metric, a.at, a.contract))
    elif a.command == 'evaluate':
        _print(evaluate(a.db))
    elif a.command == 'report':
        print(export(a.db, a.out))
    elif a.command == 'verify':
        from . import rules
        checks = verify(a.db)
        if hasattr(rules, 'verify_decisions'):
            checks += rules.verify_decisions(a.db)
        target = rules.verify_target(a.db, a.sandbox) \
            if a.sandbox and hasattr(rules, 'verify_target') else []
        if a.out:
            out = Path(a.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps({'integrity_problems': checks, 'target': target},
                                      ensure_ascii=False, indent=2), encoding='utf-8')
            out.with_suffix('.md').write_text(
                '# 独立对账\n\n' + '\n'.join(f'- {x}' for x in target), encoding='utf-8')
        _print({'integrity_problems': checks, 'target': target})
    elif a.command == 'delivery-prepare':
        from .delivery import prepare
        _print(prepare(a.db, a.id, a.request,
                       json.loads(Path(a.values).read_text(encoding='utf-8'))))
    elif a.command == 'delivery-release':
        from .delivery import release
        _print(release(a.db, a.id,
                       json.loads(Path(a.values).read_text(encoding='utf-8')), a.out))
    elif a.command == 'usage-record':
        from .usage import record
        rows = [json.loads(line)
                for line in Path(a.events).read_text(encoding='utf-8').splitlines()
                if line.strip()]
        _print(record(a.db, rows))
    elif a.command == 'usage-audit':
        from .usage import audit
        _print(audit(a.db, a.id))
    else:
        print(action(a.command, a.db, sandbox=getattr(a, 'sandbox', None),
                     out=getattr(a, 'out', None)))


if __name__ == '__main__':
    main()
