import json
from pathlib import Path

import pytest

from purposegate.ingest import initialize
from purposegate.sampling import evaluate_manual, record_frame


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def test_manual_sample_has_real_ids_and_probabilities(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'sample-config.json')
    assert record_frame(db, FIX / 'sample-frame.json') == 'frame-S1'
    assert record_frame(db, FIX / 'sample-frame.json') == 'frame-S1'
    result = evaluate_manual(db, FIX / 'sample-manual.json')
    assert result == evaluate_manual(db, FIX / 'sample-manual.json')
    assert result['feasible'] is True
    assert len(result['selected']['A']['ids']) == 2
    assert len(result['selected']['B']['ids']) == 2
    assert result['selected']['A']['inclusion_num'] == 1
    assert result['selected']['A']['inclusion_den'] == 3
    assert (result['variance_num'], result['variance_den']) == (5, 6)
    assert (result['coverage_gap_num'], result['coverage_gap_den']) == (0, 1)


def test_manual_sample_plan_conflict_keeps_original(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'sample-config.json')
    record_frame(db, FIX / 'sample-frame.json')
    evaluate_manual(db, FIX / 'sample-manual.json')
    plan = json.loads((FIX / 'sample-manual.json').read_text(encoding='utf-8'))
    plan['allocation'] = {'A': 1, 'B': 3}
    path = tmp_path / 'other.json'
    path.write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(ValueError, match='sample_plan_conflict'):
        evaluate_manual(db, path)
