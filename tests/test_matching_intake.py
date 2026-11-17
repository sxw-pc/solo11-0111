import json
from pathlib import Path

import pytest

from purposegate.ingest import initialize
from purposegate.matching import evaluate_manual, record


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def test_manual_matching_has_real_pairs_and_claims(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'link-config.json')
    assert record(db, FIX / 'link-manifest.json') == 'link-S1'
    assert record(db, FIX / 'link-manifest.json') == 'link-S1'
    result = evaluate_manual(db, FIX / 'link-manual.json')
    assert result == evaluate_manual(db, FIX / 'link-manual.json')
    assert result['total_score'] == 25
    assert result['minimum_score'] == 5
    assert result['pairs'][0]['claims'] == ['c11a', 'c11b']


def test_manual_matching_conflict_keeps_first_mapping(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'link-config.json')
    record(db, FIX / 'link-manifest.json')
    evaluate_manual(db, FIX / 'link-manual.json')
    plan = json.loads((FIX / 'link-manual.json').read_text(encoding='utf-8'))
    plan['pairs'] = [
        {'left': 'a1', 'right': 'b2'},
        {'left': 'a2', 'right': 'b3'},
        {'left': 'a3', 'right': 'b1'},
    ]
    path = tmp_path / 'other.json'
    path.write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(ValueError, match='link_plan_conflict'):
        evaluate_manual(db, path)
