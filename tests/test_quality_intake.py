import json
from pathlib import Path

import pytest

from purposegate.ingest import initialize
from purposegate.quality import evaluate_manual, record


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def test_quality_manual_repair_is_real_table_and_replayable(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'quality-config.json')
    assert record(db, FIX / 'quality-source.json') == 'quality-S1'
    assert record(db, FIX / 'quality-source.json') == 'quality-S1'
    result = evaluate_manual(db, FIX / 'quality-manual.json')
    assert result == evaluate_manual(db, FIX / 'quality-manual.json')
    assert result['valid'] is True
    assert result['changed_cells'] == 2
    assert result['trust_cost'] == 20


def test_quality_manual_conflict_keeps_first_repair(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'quality-config.json')
    record(db, FIX / 'quality-source.json')
    evaluate_manual(db, FIX / 'quality-manual.json')
    plan = json.loads((FIX / 'quality-manual.json').read_text(encoding='utf-8'))
    plan['rows'][0]['values']['region'] = 'East'
    path = tmp_path / 'other.json'
    path.write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(ValueError, match='quality_plan_conflict'):
        evaluate_manual(db, path)
