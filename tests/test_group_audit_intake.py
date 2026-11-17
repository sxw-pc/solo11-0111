import json
from pathlib import Path

import pytest

from purposegate.group_audit import raw_summary, record
from purposegate.ingest import initialize


FIX = Path(__file__).resolve().parent.parent / 'fixtures'


def test_raw_group_audit_freezes_case_mix(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'group-audit-config.json')
    assert record(db, FIX / 'group-audit-matrix.json') == 'audit-S1'
    assert record(db, FIX / 'group-audit-matrix.json') == 'audit-S1'
    result = raw_summary(db, 'audit-S1')
    assert result['groups']['A']['rate_num'] == 41
    assert result['groups']['A']['rate_den'] == 50
    assert result['groups']['B']['rate_num'] == 9
    assert result['groups']['B']['rate_den'] == 50
    assert (result['gap_num'], result['gap_den']) == (16, 25)


def test_group_audit_rejects_same_id_different_counts(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'group-audit-config.json')
    record(db, FIX / 'group-audit-matrix.json')
    doc = json.loads((FIX / 'group-audit-matrix.json').read_text(encoding='utf-8'))
    doc['cells'][0]['allowed'] += 1
    path = tmp_path / 'conflict.json'
    path.write_text(json.dumps(doc), encoding='utf-8')
    with pytest.raises(ValueError, match='group_audit_source_conflict'):
        record(db, path)
