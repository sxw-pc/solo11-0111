import json
from pathlib import Path

import pytest

from purposegate.ingest import initialize,load
from purposegate.rules import evaluate
from purposegate.delivery import prepare,release
from purposegate.usage import record,audit

FIX=Path(__file__).resolve().parent.parent/'fixtures'

def ready(tmp_path):
    db=tmp_path/'ledger.db';initialize(db,FIX/'config.json');load(db,FIX/'events.jsonl');evaluate(db)
    return db

def test_delivery_release_is_run_bound_and_field_exact(tmp_path):
    db=ready(tmp_path)
    values=json.loads((FIX/'delivery-values.json').read_text(encoding='utf-8'))
    assert prepare(db,'PKG-1','req-1',values)['state']=='prepared'
    with pytest.raises(ValueError,match='delivery_field_mismatch'):
        prepare(db,'PKG-2','req-1',{'score_band':'A','name':'secret'})
    with pytest.raises(ValueError,match='delivery_identity_conflict'):
        prepare(db,'PKG-1','req-1',{'score_band':'B'})
    out=tmp_path/'released.json'
    assert release(db,'PKG-1',values,out)['state']=='released'
    assert json.loads(out.read_text(encoding='utf-8'))['fields']==values

def test_delivery_stale_authorization_and_modified_bytes(tmp_path):
    db=ready(tmp_path);prepare(db,'PKG-1','req-1',{'score_band':'A'})
    with pytest.raises(ValueError,match='delivery_bytes_changed'):
        release(db,'PKG-1',{'score_band':'B'},tmp_path/'x.json')
    evaluate(db)
    with pytest.raises(ValueError,match='delivery_authorization_stale'):
        release(db,'PKG-1',{'score_band':'A'},tmp_path/'x.json')

def test_usage_audit_changes_outcome_after_revocation(tmp_path):
    db=ready(tmp_path);prepare(db,'PKG-1','req-1',{'score_band':'A'})
    release(db,'PKG-1',{'score_band':'A'},tmp_path/'released.json')
    first={'event_key':'u1','package_id':'PKG-1','person':'p1','field':'score_band',
           'purpose':'analytics','at':'2026-06-02T00:00:00Z'}
    assert record(db,[first])==1
    assert audit(db,'PKG-1')['status']=='compliant'
    revoke={'event_key':'revoked','subject':'req-1','kind':'revoke','person':'p1','at':'2026-06-03T00:00:00Z'}
    p=tmp_path/'revoke.jsonl';p.write_text(json.dumps(revoke),encoding='utf-8');load(db,p)
    second={**first,'event_key':'u2','at':'2026-06-04T00:00:00Z'}
    record(db,[second])
    result=audit(db,'PKG-1')
    assert result['status']=='violation'
    assert {x['event_key']:x['status'] for x in result['events']}=={'u1':'compliant','u2':'violation'}

def test_usage_does_not_trust_missing_delivery_or_conflicting_event(tmp_path):
    db=ready(tmp_path);prepare(db,'PKG-1','req-1',{'score_band':'A'})
    release(db,'PKG-1',{'score_band':'A'},tmp_path/'released.json')
    row={'event_key':'u1','package_id':'PKG-1','person':'p1','field':'score_band',
         'purpose':'analytics','at':'2026-06-02T00:00:00Z'}
    record(db,[row]);(tmp_path/'released.json').unlink()
    assert audit(db,'PKG-1')['status']=='uncertain'
    with pytest.raises(ValueError,match='usage_identity_conflict'):
        record(db,[{**row,'field':'name'}])
