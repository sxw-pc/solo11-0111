import json
import sqlite3
from pathlib import Path

import pytest

from purposegate.cohort import record_corrections, record_snapshot


FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures'


def test_cohort_snapshot_and_correction_are_source_evidence_only(tmp_path):
    db = tmp_path / 'cohort.db'
    assert record_snapshot(db, FIXTURE / 'cohort-snapshot.json') == 2
    assert record_corrections(db, FIXTURE / 'cohort-corrections.jsonl') == 1
    assert record_snapshot(db, FIXTURE / 'cohort-snapshot.json') == 0
    assert record_corrections(db, FIXTURE / 'cohort-corrections.jsonl') == 0
    con = sqlite3.connect(db)
    assert con.execute('SELECT person,score FROM cohort_member ORDER BY person').fetchall() == [
        ('p1', '60.00'), ('p2', '80.00')]
    assert con.execute('SELECT old_value,new_value FROM cohort_correction').fetchone() == ('60.00', '90.00')
    assert con.execute('SELECT count(*) FROM cohort_source WHERE length(raw)>0').fetchone()[0] == 2
    con.close()


def test_conflicting_correction_event_is_atomic(tmp_path):
    db = tmp_path / 'cohort.db'
    record_snapshot(db, FIXTURE / 'cohort-snapshot.json')
    record_corrections(db, FIXTURE / 'cohort-corrections.jsonl')
    other = tmp_path / 'other.jsonl'
    other.write_text('\n'.join((
        json.dumps({'event_key':'new','cohort_id':'cohort-A','person':'p2','field':'score','old_value':'80','new_value':'81','at':'2026-06-02T10:00:00Z'}),
        json.dumps({'event_key':'cohort-fix-1','cohort_id':'cohort-A','person':'p1','field':'score','old_value':'60','new_value':'70','at':'2026-06-02T09:00:00Z'}),
    )), encoding='utf-8')
    with pytest.raises(ValueError, match='correction_event_conflict'):
        record_corrections(db, other)
    con = sqlite3.connect(db)
    assert con.execute('SELECT count(*) FROM cohort_correction').fetchone()[0] == 1
    con.close()
