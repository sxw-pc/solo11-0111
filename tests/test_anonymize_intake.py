import json
from pathlib import Path

import pytest

from purposegate.anonymize import evaluate_manual, record
from purposegate.ingest import initialize


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def test_manual_anonymization_records_real_transformed_groups(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'anon-config.json')
    assert record(db, FIX / 'anon-source.json') == 'micro-S1'
    assert record(db, FIX / 'anon-source.json') == 'micro-S1'
    result = evaluate_manual(db, FIX / 'anon-manual.json')
    assert result == evaluate_manual(db, FIX / 'anon-manual.json')
    assert result['valid'] is True
    assert result['loss'] == 6
    assert result['groups'][0]['qi'] == {'age': '20s', 'zip': '100'}


def test_manual_grouping_can_be_valid_for_k_but_fail_l(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'anon-config.json')
    record(db, FIX / 'anon-source.json')
    plan = json.loads((FIX / 'anon-manual.json').read_text(encoding='utf-8'))
    plan['plan_id'] = 'anon-manual-l2'
    plan['l_distinct'] = 2
    path = tmp_path / 'l2.json'
    path.write_text(json.dumps(plan), encoding='utf-8')
    result = evaluate_manual(db, path)
    assert result['valid'] is False
    assert result['groups'][0]['sensitive_distinct'] == 1
    plan['groups'] = [['r1', 'r3'], ['r2', 'r4'], ['r5', 'r6']]
    path.write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(ValueError, match='anon_plan_conflict'):
        evaluate_manual(db, path)
