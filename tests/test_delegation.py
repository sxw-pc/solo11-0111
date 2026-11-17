import json
from pathlib import Path

import pytest

from purposegate.ingest import initialize, load
from purposegate.report import export, verify
from purposegate.rules import evaluate
from purposegate.store import connect
from purposegate.timeline import resolve_delegation

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def ready(tmp_path):
    db = tmp_path / 'delegation.db'
    initialize(db, FIX / 'delegation-config.json')
    load(db, FIX / 'delegation-events.jsonl')
    return db


def detail(db, subject):
    row = connect(db).execute(
        'SELECT detail FROM decision WHERE subject=? ORDER BY id DESC LIMIT 1', (subject,)).fetchone()
    return json.loads(row['detail'])


def chains(db, subject):
    return [dict(r) for r in connect(db).execute(
        'SELECT * FROM delegation_chain WHERE subject=? ORDER BY id', (subject,))]


def links(db, subject):
    return [dict(r) for r in connect(db).execute(
        'SELECT l.* FROM delegation_link l JOIN delegation_chain c ON c.id=l.chain_id '
        'WHERE c.subject=? ORDER BY l.chain_id,l.level', (subject,))]


def remake(tmp_path, event_overrides=None, config_overrides=None):
    """Build an isolated batch from the shared fixture with small edits."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = json.loads((FIX / 'delegation-config.json').read_text(encoding='utf-8'))
    if config_overrides:
        config_overrides(config)
    cfg_path = tmp_path / 'config.json'
    cfg_path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    rows = [json.loads(line) for line in
            (FIX / 'delegation-events.jsonl').read_text(encoding='utf-8').splitlines()]
    if event_overrides:
        rows = event_overrides(rows)
    events = tmp_path / 'events.jsonl'
    events.write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows), encoding='utf-8')
    db = tmp_path / 'made.db'
    initialize(db, cfg_path); load(db, events)
    return db, rows, config


def test_immediate_propagation_rejects_after_recall_and_fixed_term_survives(tmp_path):
    db = ready(tmp_path)
    result = evaluate(db)
    # Same shape, same 12:00 recall, same 13:00 request: only the contract differs.
    assert result['req-immediate'] == '拒绝'
    assert result['req-fixed'] == '允许'
    assert detail(db, 'req-fixed')['allowed_fields'] == ['score_band']
    fixed_links = links(db, 'req-fixed')
    assert [row['event_key'] for row in fixed_links] == ['fix-src', 'fix-child']
    assert fixed_links[-1]['status'] == 'protected'
    assert json.loads(fixed_links[-1]['detail'])['effective_until'].startswith('2026-06-01T16:00:00')
    immediate = chains(db, 'req-immediate')[0]
    assert immediate['contract'] == 'immediate'
    assert json.loads(immediate['detail'])['issues'] == ['delegation_revoked']


def test_contract_is_frozen_at_source_and_cannot_be_switched(tmp_path):
    db = ready(tmp_path)
    assert evaluate(db)['req-contract-switch'] == '无法判定'
    chain = chains(db, 'req-contract-switch')[0]
    # The source said immediate even though the child claimed fixed_term.
    assert chain['contract'] == 'immediate'
    assert chain['contract_event_key'] == 'cs-src'
    assert chain['contract_source_sha']
    assert 'delegation_contract_switch' in json.loads(chain['detail'])['issues']


def test_multilevel_subdelegation_narrows_fields_and_purpose(tmp_path):
    db = ready(tmp_path)
    result = evaluate(db)
    assert result['req-multilevel'] == '允许'
    link_rows = links(db, 'req-multilevel')
    assert [row['event_key'] for row in link_rows] == ['ml-src', 'ml-l1', 'ml-l2']
    # The last link narrowed to score; score_band therefore cannot be delivered.
    assert json.loads(link_rows[-1]['field_intersection']) == ['score']
    assert detail(db, 'req-multilevel')['allowed_fields'] == ['score']
    assert result['req-purpose-widen'] == '拒绝'
    assert 'delegation_purpose_widened' in json.loads(
        chains(db, 'req-purpose-widen')[0]['detail'])['issues']


def test_parallel_chains_are_not_union_into_a_fake_license(tmp_path):
    db = ready(tmp_path)
    result = evaluate(db)
    # req-parallel: two independent chains, each complete on its own.
    assert result['req-parallel'] == '允许'
    assert detail(db, 'req-parallel')['allowed_fields'] == ['score', 'score_band']
    # req-split: one chain licenses score, the other region; neither may be
    # combined into {score, region} for the same request.
    assert result['req-split'] == '无法判定'
    split = detail(db, 'req-split')
    assert split['allowed_fields'] == []
    assert 'delegation_chain_conflict' in split['issues']
    per_chain = [json.loads(c['detail'])['field_intersection'] for c in chains(db, 'req-split')]
    assert sorted(per_chain) == [['region'], ['score']]


def test_conflicting_chains_and_divergent_event_bodies_are_uncertain(tmp_path):
    db = ready(tmp_path)
    result = evaluate(db)
    assert result['req-conflict'] == '无法判定'
    assert 'delegation_chain_conflict' in detail(db, 'req-conflict')['issues']
    assert result['req-conflict-event'] == '无法判定'
    event_chain = chains(db, 'req-conflict-event')[0]
    assert 'delegation_source_conflict' in json.loads(event_chain['detail'])['issues']
    assert event_chain['status'] == 'uncertain'


def test_cycle_parent_missing_and_issuer_provenance_are_never_licensed(tmp_path):
    db = ready(tmp_path)
    result = evaluate(db)
    assert result['req-cycle'] == '无法判定'
    cycle_chain = chains(db, 'req-cycle')[0]
    assert 'delegation_cycle' in json.loads(cycle_chain['detail'])['issues']
    assert result['req-nosource'] == '无法判定'
    assert 'delegation_source_missing' in json.loads(
        chains(db, 'req-nosource')[0]['detail'])['issues']
    assert result['req-issuer'] == '无法判定'
    assert 'delegation_source_mismatch' in json.loads(
        chains(db, 'req-issuer')[0]['detail'])['issues']
    assert result['req-overbroad'] == '拒绝'
    assert 'delegation_overbroad' in json.loads(
        chains(db, 'req-overbroad')[0]['detail'])['issues']
    assert result['req-nodelegate'] == '拒绝'
    assert 'delegation_not_allowed' in json.loads(
        chains(db, 'req-nodelegate')[0]['detail'])['issues']


def test_half_open_boundary_and_same_instant_revocation(tmp_path):
    db = ready(tmp_path)
    result = evaluate(db)
    # until 16:00 is open at the right; a request exactly at 16:00 is expired.
    assert result['req-boundary'] == '拒绝'
    assert 'delegation_expired' in json.loads(chains(db, 'req-boundary')[0]['detail'])['issues']
    # A recall at the very instant of the request wins the tie.
    assert result['req-sametime'] == '拒绝'
    assert 'delegation_revoked' in json.loads(chains(db, 'req-sametime')[0]['detail'])['issues']


def test_both_contracts_block_new_subdelegation_after_recall(tmp_path):
    db = ready(tmp_path)
    result = evaluate(db)
    assert result['req-redelegate-immediate'] == '拒绝'
    assert result['req-redelegate-fixed'] == '拒绝'
    for subject in ('req-redelegate-immediate', 'req-redelegate-fixed'):
        issues = json.loads(chains(db, subject)[0]['detail'])['issues']
        assert 'delegation_new_signing_after_revocation' in issues
    # A protected child survives, but cannot itself create a new grandchild.
    assert result['req-fixed-grandchild'] == '拒绝'
    grandchild = json.loads(chains(db, 'req-fixed-grandchild')[0]['detail'])['issues']
    assert 'delegation_new_signing_after_revocation' in grandchild


def test_fixed_term_protection_requires_a_satisfying_child_clause(tmp_path):
    def drop_clause(rows):
        return [{k: v for k, v in r.items() if k != 'irrevocable_until'} for r in rows]
    db, _rows, _cfg = remake(tmp_path, event_overrides=drop_clause)
    result = evaluate(db)
    assert result['req-fixed'] == '拒绝'
    issues = json.loads(chains(db, 'req-fixed')[0]['detail'])['issues']
    assert 'delegation_revoked' in issues


def test_protected_child_stops_being_valid_past_its_clause(tmp_path):
    db = ready(tmp_path)
    result = evaluate(db)
    # req-fixed is inside the 16:00 clause; req-fixed-late requests at 16:30.
    assert result['req-fixed'] == '允许'
    assert result['req-fixed-late'] == '拒绝'
    issues = json.loads(chains(db, 'req-fixed-late')[0]['detail'])['issues']
    assert 'delegation_revoked' in issues


def test_delegation_and_direct_authorization_partition_separately(tmp_path):
    db = ready(tmp_path)
    result = evaluate(db)
    # dm1 is delegated, dm2 has a direct grant; the union must not authorize.
    assert result['req-mixed'] == '拒绝'
    detail_mixed = detail(db, 'req-mixed')
    assert detail_mixed['allowed_fields'] == []
    assert detail_mixed['partitions']['dm1'] == {'allowed_fields': ['score_band'], 'status': '允许'}
    assert detail_mixed['partitions']['dm2'] == {'allowed_fields': [], 'status': '允许'}
    assert 'no_common_field' in detail_mixed['issues']


def test_unrooted_child_is_parent_missing_not_reachable(tmp_path):
    db = ready(tmp_path)
    result = evaluate(db)
    assert result['req-orphan'] == '无法判定'
    chain = chains(db, 'req-orphan')[0]
    assert chain['status'] == 'uncertain'
    assert json.loads(chain['detail'])['issues'] == ['delegation_parent_missing']


def test_recall_before_signing_blocks_even_fixed_term(tmp_path):
    def shift_recall(rows):
        for r in rows:
            if r['event_key'] == 'fix-rev':
                r['at'] = '2026-06-01T10:30:00Z'  # before fix-child was signed
        return rows
    db, _rows, _cfg = remake(tmp_path, event_overrides=shift_recall)
    assert evaluate(db)['req-fixed'] == '拒绝'


def test_evidence_lists_each_level_source_sha_intersection_and_effect(tmp_path):
    db = ready(tmp_path)
    evaluate(db)
    path = export(db, tmp_path / 'report.json')
    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['delegation_chains']
    chain = next(c for c in data['delegation_chains'] if c['subject'] == 'req-fixed')
    assert chain['contract'] == 'fixed_term' and chain['contract_source_sha']
    assert [link['event_key'] for link in chain['links']] == ['fix-src', 'fix-child']
    assert all(link['source_sha'] for link in chain['links'])
    assert all(link['field_intersection'] for link in chain['links'])
    text = path.with_suffix('.md').read_text(encoding='utf-8')
    assert '委托链证据' in text and '来源SHA' in text
    # Verify the frozen evidence against raw sources.
    assert verify(db) == []
    # A report export must not rewrite the historical run.
    run_id = data['run']['id']
    export(db, tmp_path / 'again.json')
    assert connect(db).execute('SELECT count(*) FROM run').fetchone()[0] == run_id


def test_delegation_change_flows_causally_into_person_partitions(tmp_path):
    db = ready(tmp_path)
    assert evaluate(db)['req-fixed'] == '允许'
    assert detail(db, 'req-fixed')['partitions']['ds2']['status'] == '允许'

    def tighten(rows):
        for r in rows:
            if r['event_key'] == 'fix-child':
                r['fields'] = []
        return rows
    db2, _rows, _cfg = remake(tmp_path / 'tight', event_overrides=tighten)
    result = evaluate(db2)
    assert result['req-fixed'] == '拒绝'
    detail2 = detail(db2, 'req-fixed')
    assert detail2['partitions']['ds2'] == {'allowed_fields': [], 'status': '拒绝'}
    assert detail2['allowed_fields'] == []


def test_embedded_source_must_match_the_parent_body(tmp_path):
    def embed_divergence(rows):
        for row in rows:
            if row['event_key'] == 'nw-l1':
                # Same key as the parent but a narrower field list: this is a
                # different body, not the signed authorization it points to.
                row['source'] = {
                    'event_key': 'nw-src', 'kind': 'delegate_source', 'delegator': 'ds13',
                    'delegatee': 'nwa', 'purpose': 'analytics', 'fields': ['score'],
                    'from': '2026-06-01T10:00:00Z', 'until': '2026-06-01T18:00:00Z',
                    'at': '2026-06-01T10:00:00Z', 'revocation_effect': 'immediate',
                    'delegate_allowed': True, 'subject': 'req-narrow', 'person': 'ds13'}
        return rows
    db, _rows, _cfg = remake(tmp_path / 'embed', event_overrides=embed_divergence)
    assert evaluate(db)['req-narrow'] == '无法判定'
    issues = json.loads(chains(db, 'req-narrow')[0]['detail'])['issues']
    assert 'delegation_source_conflict' in issues


def test_embedded_source_matching_parent_is_accepted(tmp_path):
    def embed_matching(rows):
        parent = next(r for r in rows if r['event_key'] == 'nw-src')
        for row in rows:
            if row['event_key'] == 'nw-l1':
                row['source'] = dict(parent)
        return rows
    db, _rows, _cfg = remake(tmp_path / 'embed-ok', event_overrides=embed_matching)
    assert evaluate(db)['req-narrow'] == '允许'


def test_subject_isolation_keeps_other_subjects_evidence_out(tmp_path):
    """Delegation events on one subject cannot authorize another request."""
    db = ready(tmp_path)
    evaluate(db)
    state = {row['subject']: row['status'] for row in connect(db).execute(
        'SELECT subject,status FROM decision WHERE run_id=(SELECT max(id) FROM run)')}
    assert state['req-immediate'] == '拒绝' and state['req-fixed'] == '允许'
    # A fixed-term chain for ds2 must not appear under req-immediate's evidence.
    keys = {link['event_key'] for link in links(db, 'req-immediate')}
    assert 'fix-src' not in keys and 'fix-child' not in keys
    assert 'imm-src' in keys and 'imm-child' in keys


def test_missing_delegation_permission_flag_is_uncertain(tmp_path):
    def drop_flag(rows):
        return [{k: v for k, v in r.items() if k != 'delegate_allowed'} for r in rows]
    db, _rows, _cfg = remake(tmp_path / 'flag', event_overrides=drop_flag)
    assert evaluate(db)['req-narrow'] == '无法判定'
    issues = json.loads(chains(db, 'req-narrow')[0]['detail'])['issues']
    assert 'delegation_flag_missing' in issues


def test_ingest_keeps_defective_delegation_event_as_issue(tmp_path):
    db = ready(tmp_path)
    con = connect(db)
    issue = con.execute("SELECT * FROM issue WHERE code='delegation_source_missing'").fetchone()
    assert issue and issue['subject'] == 'req-nosource'
    # It is still stored so the chain evidence can show the defective step.
    stored = con.execute("SELECT * FROM raw_event WHERE event_key='ns-l1'").fetchone()
    assert stored and stored['kind'] == 'delegate'


def test_delegation_ingest_rejects_unknown_kind(tmp_path):
    db = ready(tmp_path)
    p = tmp_path / 'bad.jsonl'
    p.write_text(json.dumps({'event_key': 'zz', 'subject': 'req-1', 'kind': 'subdelegate',
                             'at': '2026-06-01'}), encoding='utf-8')
    assert load(db, p) == 0
    assert connect(db).execute(
        "SELECT 1 FROM issue WHERE code='parse_error' ORDER BY id DESC LIMIT 1").fetchone()


def test_offline_cli_exposes_delegation_evidence(tmp_path, capsys):
    import purposegate.__main__ as cli
    work = tmp_path / 'cli'
    import sys
    argv = sys.argv
    try:
        sys.argv = ['purposegate', 'demo', '--workdir', str(work)]
        cli.main()
    finally:
        sys.argv = argv
    out = capsys.readouterr().out
    assert 'req-fixed' in out and 'req-immediate' in out
    assert (work / 'delegation-report.json').exists()
    db = work / 'delegation.db'
    from purposegate.report import export_chains
    payload = export_chains(db, 'req-fixed')
    assert payload['chains'][0]['contract'] == 'fixed_term'
    assert payload['chains'][0]['links'][-1]['status'] == 'protected'


def test_direct_authorization_is_unchanged_by_delegation_evidence(tmp_path):
    """The original fixtures contain no delegation events and keep their result."""
    db = tmp_path / 'direct.db'
    initialize(db, FIX / 'config.json'); load(db, FIX / 'events.jsonl')
    assert evaluate(db) == {'req-1': '允许', 'req-2': '拒绝'}
    evaluator = resolve_delegation(
        [{'event_key': 'g1', 'subject': 'req-1', 'kind': 'grant', 'person': 'p1',
          'purpose': 'research', 'fields': ['score'], 'from': '2026-01-01',
          'until': '2027-01-01', 'at': '2026-01-01'}],
        '2026-06-01', 'p1', 'vendor', 'analytics', ['score'],
        {'analytics': 'research', 'research': None}, {'score_band': ['score']},
        {'name', 'score', 'score_band'})
    assert evaluator['status'] == '拒绝' and 'no_valid_delegation' in evaluator['issues']


def test_delegation_requires_declared_final_user_and_purpose(tmp_path):
    db = ready(tmp_path)
    cfg = json.loads(connect(db).execute('SELECT config_raw FROM batch').fetchone()[0])
    assert cfg['subjects']['req-fixed']['delegatees'] == {'ds2': 'vfb'}
    assert cfg['subjects']['req-fixed']['purpose'] == 'analytics'
    assert evaluate(db)['req-fixed'] == '允许'
    # With no delegatee declaration the direct path applies and finds no grant.
    con = connect(db)
    cfg['subjects']['req-fixed'].pop('delegatees')
    con.execute('UPDATE batch SET config_raw=?', (json.dumps(cfg).encode(),))
    con.commit(); con.close()
    assert evaluate(db)['req-fixed'] == '拒绝'
    assert detail(db, 'req-fixed')['partitions']['ds2']['allowed_fields'] == []


def test_pytest_covers_every_named_scenario(tmp_path):
    db = ready(tmp_path)
    result = evaluate(db)
    assert set(result) == {
        'req-immediate', 'req-fixed', 'req-multilevel', 'req-parallel', 'req-conflict',
        'req-cycle', 'req-boundary', 'req-sametime', 'req-redelegate-immediate',
        'req-redelegate-fixed', 'req-overbroad', 'req-purpose-widen', 'req-narrow',
        'req-nosource', 'req-conflict-event', 'req-issuer', 'req-contract-switch',
        'req-split', 'req-nodelegate', 'req-fixed-late', 'req-mixed', 'req-orphan',
        'req-fixed-grandchild'}
    assert result['req-narrow'] == '允许'
    assert detail(db, 'req-narrow')['allowed_fields'] == ['score']
