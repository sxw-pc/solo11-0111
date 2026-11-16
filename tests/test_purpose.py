import json
from pathlib import Path
from purposegate.ingest import initialize,load
from purposegate.rules import evaluate,_ancestors,verify_decisions
from purposegate.report import export,verify
from purposegate.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def setup(tmp):
    db=tmp/'x.db';initialize(db,FIX/'config.json');load(db,FIX/'events.jsonl');return db
def test_demo(tmp_path):assert evaluate(setup(tmp_path))=={'req-1':'允许','req-2':'拒绝'}
def test_idempotent(tmp_path):assert load(setup(tmp_path),FIX/'events.jsonl')==0
def test_purpose_prefix_not_allowed(tmp_path):
    db=setup(tmp_path);con=connect(db);cfg=json.loads(con.execute('SELECT config_raw FROM batch').fetchone()[0]);cfg['subjects']['req-1']['purpose']='research_extra';con.execute('UPDATE batch SET config_raw=?',(json.dumps(cfg).encode(),));con.commit();con.close()
    assert evaluate(db)['req-1']=='无法判定'
def test_required_denied(tmp_path):
    db=setup(tmp_path);con=connect(db);cfg=json.loads(con.execute('SELECT config_raw FROM batch').fetchone()[0]);cfg['subjects']['req-1']['required']=['name'];con.execute('UPDATE batch SET config_raw=?',(json.dumps(cfg).encode(),));con.commit();con.close()
    assert evaluate(db)['req-1']=='拒绝'
def test_mixed_subjects(tmp_path):
    db=setup(tmp_path);con=connect(db);cfg=json.loads(con.execute('SELECT config_raw FROM batch').fetchone()[0]);cfg['subjects']['req-1']['persons']=['p1','p2'];con.execute('UPDATE batch SET config_raw=?',(json.dumps(cfg).encode(),));con.commit();con.close()
    assert evaluate(db)['req-1']=='拒绝'
def test_lineage_diamond_and_cycle():
    assert _ancestors('d',{'d':['b','c'],'b':['a'],'c':['a']})=={'a','b','c','d'}
    import pytest
    with pytest.raises(ValueError):_ancestors('a',{'a':['b'],'b':['a']})
def test_validity_boundary(tmp_path):
    db=setup(tmp_path);con=connect(db);cfg=json.loads(con.execute('SELECT config_raw FROM batch').fetchone()[0]);cfg['subjects']['req-1']['at']='2027-01-01';con.execute('UPDATE batch SET config_raw=?',(json.dumps(cfg).encode(),));con.commit();con.close()
    assert evaluate(db)['req-1']=='拒绝'
def test_report_and_verify(tmp_path):
    db=setup(tmp_path);evaluate(db);p=export(db,tmp_path/'out.json');assert p.exists() and p.with_suffix('.md').exists() and verify(db)==[]
def test_unknown_version(tmp_path):
    db=setup(tmp_path);con=connect(db);cfg=json.loads(con.execute('SELECT config_raw FROM batch').fetchone()[0]);cfg['subjects']['req-1']['version']='d2';con.execute('UPDATE batch SET config_raw=?',(json.dumps(cfg).encode(),));con.commit();con.close()
    assert evaluate(db)['req-1']=='无法判定'
def test_explicit_field_rename_mapping(tmp_path):
    db=setup(tmp_path);con=connect(db);cfg=json.loads(con.execute('SELECT config_raw FROM batch').fetchone()[0])
    cfg['subjects']['req-1']['version']='d0';cfg['subjects']['req-1']['fields']=['band_old'];cfg['subjects']['req-1']['required']=['band_old']
    cfg['version_mappings']={'d0':{'band_old':'score_band'}}
    con.execute('UPDATE batch SET config_raw=?',(json.dumps(cfg).encode(),));con.commit();con.close()
    assert evaluate(db)['req-1']=='允许'
def test_partial_field_mapping_remains_unknown(tmp_path):
    db=setup(tmp_path);con=connect(db);cfg=json.loads(con.execute('SELECT config_raw FROM batch').fetchone()[0])
    cfg['subjects']['req-1']['version']='d0';cfg['subjects']['req-1']['fields']=['unmapped_old'];cfg['version_mappings']={'d0':{'band_old':'score_band'}}
    con.execute('UPDATE batch SET config_raw=?',(json.dumps(cfg).encode(),));con.commit();con.close()
    assert evaluate(db)['req-1']=='无法判定'
def test_grant_narrowing(tmp_path):
    db=setup(tmp_path);p=tmp_path/'n.jsonl';p.write_text(json.dumps({'event_key':'n1','subject':'req-1','kind':'narrow','person':'p1','fields':['name'],'at':'2026-05-01'}));load(db,p)
    assert evaluate(db)['req-1']=='拒绝'
def test_revocation_after_request_does_not_block(tmp_path):
    db=setup(tmp_path);con=connect(db);cfg=json.loads(con.execute('SELECT config_raw FROM batch').fetchone()[0]);cfg['subjects']['req-2']['at']='2026-04-01';con.execute('UPDATE batch SET config_raw=?',(json.dumps(cfg).encode(),));con.commit();con.close()
    assert evaluate(db)['req-2']=='允许'
def test_unknown_lineage_ancestor(tmp_path):
    db=setup(tmp_path);con=connect(db);cfg=json.loads(con.execute('SELECT config_raw FROM batch').fetchone()[0]);cfg['lineage']['score_band']=['missing'];con.execute('UPDATE batch SET config_raw=?',(json.dumps(cfg).encode(),));con.commit();con.close()
    assert evaluate(db)['req-1']=='无法判定'
def test_event_identity_conflict(tmp_path):
    db=setup(tmp_path);p=tmp_path/'c.jsonl';p.write_text(json.dumps({'event_key':'g1','subject':'req-1','kind':'revoke','person':'p1','at':'2026-01-01'}));load(db,p)
    assert connect(db).execute("SELECT count(*) FROM issue WHERE code='identity_conflict'").fetchone()[0]==1
def test_report_redacts_values(tmp_path):
    db=setup(tmp_path);evaluate(db);p=export(db,tmp_path/'r.json');text=p.read_text(encoding='utf-8');assert 'allowed_fields' in text and 'source_file' not in text
def test_independent_recheck_detects_tamper(tmp_path):
    db=setup(tmp_path);evaluate(db);assert verify_decisions(db)==[]
    con=connect(db);con.execute("UPDATE decision SET status='允许' WHERE subject='req-2'");con.commit();con.close()
    assert 'decision_mismatch:req-2' in verify_decisions(db)
