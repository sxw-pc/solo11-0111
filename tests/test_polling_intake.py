import json
from pathlib import Path

import pytest

from purposegate.ingest import initialize
from purposegate.polling import evaluate_manual, record


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def test_manual_poll_schedule_has_exact_staleness_and_gap(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'poll-config.json')
    assert record(db, FIX / 'poll-forecast.json') == 'forecast-S1'
    assert record(db, FIX / 'poll-forecast.json') == 'forecast-S1'
    result = evaluate_manual(db, FIX / 'poll-manual.json')
    assert result == evaluate_manual(db, FIX / 'poll-manual.json')
    assert result['feasible'] is True
    assert result['poll_count'] == 2
    assert result['max_gap'] == 3
    assert (result['expected_stale_num'], result['expected_stale_den']) == (21, 10)


def test_manual_poll_conflict_keeps_first_plan(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'poll-config.json')
    record(db, FIX / 'poll-forecast.json')
    evaluate_manual(db, FIX / 'poll-manual.json')
    plan = json.loads((FIX / 'poll-manual.json').read_text(encoding='utf-8'))
    plan['polls'] = [["consent-feed"], [], [], [], [], ["consent-feed"]]
    path = tmp_path / 'other.json'
    path.write_text(json.dumps(plan), encoding='utf-8')
    with pytest.raises(ValueError, match='poll_plan_conflict'):
        evaluate_manual(db, path)
