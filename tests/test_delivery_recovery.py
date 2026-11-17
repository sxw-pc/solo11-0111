"""Crash-window, recovery-route and post-recovery audit tests.

Every test works in its own ``tmp_path`` sandbox: the ledger, the released
file, the pending file and the quarantine are all created there.  The fixture
inputs under ``fixtures/`` are only read, never modified.
"""
import json
from pathlib import Path

import pytest

from purposegate import delivery
from purposegate.delivery import InjectedCrash, FAULT_WINDOWS, ROUTES, inspect, prepare, recover, release
from purposegate.ingest import initialize, load
from purposegate.rules import evaluate
from purposegate.store import connect
from purposegate.usage import audit, record

FIX = Path(__file__).resolve().parent.parent / 'fixtures'
VALUES = {'score_band': 'A'}
USE = {'event_key': 'u1', 'package_id': 'PKG-1', 'person': 'p1', 'field': 'score_band',
       'purpose': 'analytics', 'at': '2026-06-02T00:00:00Z'}


# --------------------------------------------------------------------------- helpers

def ready(tmp_path):
    db = tmp_path / 'ledger.db'
    initialize(db, FIX / 'config.json')
    load(db, FIX / 'events.jsonl')
    evaluate(db)
    prepare(db, 'PKG-1', 'req-1', VALUES)
    return db


def crash_during_release(db, destination, window, package_id='PKG-1'):
    """Run release until ``window`` and let the simulated crash propagate."""
    def hook(seen):
        if seen == window:
            raise InjectedCrash(seen)
    delivery.FAULT_HOOK = hook
    try:
        with pytest.raises(InjectedCrash):
            release(db, package_id, VALUES, destination)
    finally:
        delivery.FAULT_HOOK = None


def crash_after_commit(db, destination, package_id='PKG-1'):
    crash_during_release(db, destination, 'commit', package_id)


def poke_ledger(db, sql, params=()):
    con = connect(db)
    with con:
        con.execute(sql, params)
    con.close()


def fetchone(db, sql, params=()):
    con = connect(db)
    row = con.execute(sql, params).fetchone()
    con.close()
    return row


def recovery_rows(db):
    con = connect(db)
    rows = [dict(r) for r in con.execute(
        'SELECT * FROM delivery_recovery WHERE package_id=? ORDER BY id', ('PKG-1',))]
    con.close()
    return rows


def destination_free(tmp_path, name='out.json'):
    dest = tmp_path / name
    for suffix in ('.pending', '.pending.quarantine'):
        Path(str(dest) + suffix).unlink(missing_ok=True)
    dest.unlink(missing_ok=True)
    return dest


def pending(dest):
    return Path(str(dest) + '.pending')


def quarantined(dest):
    return Path(str(dest) + '.pending.quarantine')


def stale_authorization(db):
    """A newer run without an allowing decision makes the frozen run stale."""
    poke_ledger(db, "INSERT INTO run(batch_id,status) VALUES ('purpose-demo','已判定')")


# ------------------------------------------------------------------ crash windows

@pytest.mark.parametrize('window', FAULT_WINDOWS)
def test_each_window_leaves_classifiable_evidence(tmp_path, window):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_during_release(db, dest, window)
    row = inspect(db, 'PKG-1')
    assert row['release'] is not None
    assert row['release']['status'] in ('prepared', 'committed', 'delivered')
    assert row['release']['payload_digest'] == row['release']['temp_digest']
    assert row['release']['destination'] == str(dest)
    assert set(row['release']['manifest_hash']) <= set('0123456789abcdef')
    assert len(row['release']['manifest_hash']) == 64
    # Whatever window crashed, the ledger alone is never delivery proof.
    if window != 'confirm':
        assert row['delivery_evidence']['ok'] is False


@pytest.mark.parametrize('window', ('prepare', 'temp', 'commit'))
@pytest.mark.parametrize('route', ('promote_verified', 'require_reapproval'))
def test_pending_windows_are_recoverable_on_both_routes(tmp_path, window, route):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_during_release(db, dest, window)
    assert not dest.exists()
    if window == 'prepare':
        # The intent is frozen but no pending bytes exist yet: neither route may
        # invent a delivery or claim a verified promotion.
        result = recover(db, 'PKG-1', route)
        assert result['outcome'] == 'evidence_incomplete'
        assert result['reason'] == 'temp_missing'
        assert not dest.exists()
        if route == 'require_reapproval':
            # Nothing to release: the package still returns to reapproval.
            assert inspect(db, 'PKG-1')['release_status'] == 'reapproval_required'
        else:
            assert inspect(db, 'PKG-1')['state'] == 'prepared'
        return
    result = recover(db, 'PKG-1', route)
    if route == 'promote_verified':
        assert result['outcome'] == 'delivered'
        assert result['reason'] == 'promoted_verified'
        assert json.loads(dest.read_text(encoding='utf-8'))['fields'] == VALUES
        assert not pending(dest).exists()
        assert inspect(db, 'PKG-1')['release_status'] == 'delivered'
    else:
        assert result['outcome'] == 'reapproval_required'
        assert result['reason'] == 'quarantined_for_reapproval'
        assert not dest.exists()
        assert not pending(dest).exists()
        assert quarantined(dest).is_file()
        assert inspect(db, 'PKG-1')['state'] == 'blocked'
        assert inspect(db, 'PKG-1')['release_status'] == 'reapproval_required'
    row = result['evidence']['ledger']
    assert row['payload_digest'] == row['temp_digest']


def test_commit_window_counterexample_gives_two_different_conclusions(tmp_path):
    """DB committed but not renamed: the two routes must disagree, safely."""
    promote_db = ready(tmp_path / 'promote')
    promote_dest = destination_free(tmp_path / 'promote')
    crash_after_commit(promote_db, promote_dest)
    assert inspect(promote_db, 'PKG-1')['state'] == 'released'
    assert not promote_dest.exists()
    record(promote_db, [USE])
    assert audit(promote_db, 'PKG-1')['status'] == 'uncertain'
    promoted = recover(promote_db, 'PKG-1', 'promote_verified')
    assert promoted['outcome'] == 'delivered' and promoted['delivered'] is True
    assert promote_dest.exists()
    assert audit(promote_db, 'PKG-1')['status'] == 'compliant'

    reapprove_db = ready(tmp_path / 'reapprove')
    reapprove_dest = destination_free(tmp_path / 'reapprove')
    crash_after_commit(reapprove_db, reapprove_dest)
    record(reapprove_db, [USE])
    withheld = recover(reapprove_db, 'PKG-1', 'require_reapproval')
    assert withheld['outcome'] == 'reapproval_required' and withheld['delivered'] is False
    assert not reapprove_dest.exists()
    assert inspect(reapprove_db, 'PKG-1')['state'] == 'blocked'
    assert audit(reapprove_db, 'PKG-1')['status'] == 'uncertain'


def test_rename_and_confirm_windows_are_confirmed_not_rolled_back(tmp_path):
    for window in ('rename', 'confirm'):
        for route in ROUTES:
            work = tmp_path / f'{window}-{route}'
            db = ready(work)
            dest = destination_free(work)
            crash_during_release(db, dest, window)
            assert dest.is_file()
            before = dest.read_bytes()
            result = recover(db, 'PKG-1', route)
            assert result['outcome'] == 'delivered', (window, route, result)
            assert result['reason'] in ('destination_already_identical', 'already_delivered')
            assert dest.read_bytes() == before
            assert inspect(db, 'PKG-1')['release_status'] == 'delivered'


def test_completed_delivery_is_idempotent_and_never_repeated(tmp_path):
    db = ready(tmp_path)
    dest = tmp_path / 'out.json'
    assert release(db, 'PKG-1', VALUES, dest)['state'] == 'released'
    delivered_at = fetchone(db, 'SELECT released_at FROM delivery_package WHERE id=?', ('PKG-1',))['released_at']
    before = dest.read_bytes()
    again = release(db, 'PKG-1', VALUES, dest)
    assert again['release_status'] == 'delivered' and again['delivery_evidence']['ok'] is True
    assert dest.read_bytes() == before
    assert fetchone(db, 'SELECT released_at FROM delivery_package WHERE id=?', ('PKG-1',))['released_at'] == delivered_at
    result = recover(db, 'PKG-1', 'promote_verified')
    assert result['outcome'] == 'delivered' and result['changed'] is False
    assert recovery_rows(db)[-1]['outcome'] == 'delivered'


# ------------------------------------------------------------- evidence integrity

@pytest.mark.parametrize('route', ROUTES)
def test_corrupt_pending_is_never_promoted(tmp_path, route):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_after_commit(db, dest)
    temp = pending(dest)
    temp.write_bytes(temp.read_bytes()[:-3] + b'XXX')
    result = recover(db, 'PKG-1', route)
    assert result['outcome'] == 'evidence_incomplete'
    assert result['reason'] in ('temp_digest_mismatch', 'quarantined_incomplete_temp')
    assert not dest.exists()
    if route == 'promote_verified':
        assert inspect(db, 'PKG-1')['release_status'] == 'committed'
    else:
        # The incomplete pending file is quarantined and cannot be delivered.
        assert inspect(db, 'PKG-1')['release_status'] == 'reapproval_required'
        assert quarantined(dest).is_file()


@pytest.mark.parametrize('route', ROUTES)
def test_missing_pending_is_not_delivered(tmp_path, route):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_after_commit(db, dest)
    pending(dest).unlink()
    result = recover(db, 'PKG-1', route)
    assert result['outcome'] == 'evidence_incomplete'
    assert result['reason'] == 'temp_missing'
    assert not dest.exists()


@pytest.mark.parametrize('route', ROUTES)
def test_conflicting_destination_is_never_overwritten(tmp_path, route):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_after_commit(db, dest)
    dest.write_text('{"another":"delivery"}', encoding='utf-8')
    before = dest.read_bytes()
    result = recover(db, 'PKG-1', route)
    assert result['outcome'] == 'content_conflict'
    assert result['reason'] == 'destination_content_conflict'
    assert dest.read_bytes() == before
    assert pending(dest).is_file()
    assert inspect(db, 'PKG-1')['release_status'] == 'committed'


@pytest.mark.parametrize('route', ROUTES)
def test_changed_authorization_is_never_promoted(tmp_path, route):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_after_commit(db, dest)
    stale_authorization(db)
    result = recover(db, 'PKG-1', route)
    assert result['outcome'] in ('authorization_stale', 'reapproval_required')
    assert result['outcome'] != 'delivered'
    assert not dest.exists()
    if route == 'promote_verified':
        assert result['reason'] == 'authorization_no_longer_valid'
        assert inspect(db, 'PKG-1')['release_status'] == 'committed'
        assert pending(dest).is_file()
    else:
        assert result['reason'] == 'quarantined_authorization_stale'
        assert inspect(db, 'PKG-1')['release_status'] == 'reapproval_required'
        assert quarantined(dest).is_file()


def test_ledger_hard_released_without_file_is_not_trusted(tmp_path):
    """The pre-protocol failure mode: state='released' but no destination."""
    db = ready(tmp_path)
    dest = tmp_path / 'out.json'
    poke_ledger(db, "UPDATE delivery_package SET state='released',released_at=CURRENT_TIMESTAMP,"
                    "release_status='committed',output_path=? WHERE id=?", (str(dest), 'PKG-1'))
    result = recover(db, 'PKG-1', 'promote_verified')
    assert result['outcome'] == 'evidence_incomplete'
    assert result['reason'] == 'release_evidence_absent'
    assert not dest.exists()
    assert inspect(db, 'PKG-1')['delivery_evidence']['ok'] is False


@pytest.mark.parametrize('route', ROUTES)
def test_recovery_is_idempotent(tmp_path, route):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_after_commit(db, dest)
    first = recover(db, 'PKG-1', route)
    rows_after_first = recovery_rows(db)
    assert first['recorded'] is True
    second = recover(db, 'PKG-1', route)
    assert second['outcome'] == first['outcome']
    assert second['changed'] is False and second['recorded'] is False
    assert recovery_rows(db) == rows_after_first
    if route == 'promote_verified':
        assert dest.is_file() and inspect(db, 'PKG-1')['release_status'] == 'delivered'
    else:
        assert not dest.exists() and inspect(db, 'PKG-1')['release_status'] == 'reapproval_required'


def test_recovery_record_keeps_digests_and_operation(tmp_path):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_after_commit(db, dest)
    result = recover(db, 'PKG-1', 'promote_verified')
    rows = recovery_rows(db)
    assert rows and rows[-1]['route'] == 'promote_verified'
    assert rows[-1]['outcome'] == 'delivered'
    evidence = json.loads(rows[-1]['evidence'])
    assert evidence['ledger']['payload_digest'] == result['evidence']['ledger']['payload_digest']
    assert evidence['files']['destination_digest'] == evidence['ledger']['destination_digest']
    assert evidence['authorization']['valid'] is True


def test_quarantined_file_corruption_is_detected_on_rerun(tmp_path):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_after_commit(db, dest)
    recover(db, 'PKG-1', 'require_reapproval')
    quarantined(dest).write_bytes(b'not the frozen payload')
    assert recover(db, 'PKG-1', 'require_reapproval')['reason'] == 'quarantine_digest_mismatch'
    assert recover(db, 'PKG-1', 'require_reapproval')['outcome'] == 'content_conflict'
    assert not dest.exists()


def test_other_route_after_promotion_records_a_distinct_decision(tmp_path):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_after_commit(db, dest)
    recover(db, 'PKG-1', 'promote_verified')
    result = recover(db, 'PKG-1', 'require_reapproval')
    # A delivered file is not rolled back by the other route.
    assert result['outcome'] == 'delivered' and result['changed'] is False
    assert result['recorded'] is True
    assert [r['route'] for r in recovery_rows(db)] == ['promote_verified', 'require_reapproval']
    assert dest.is_file()


def test_promote_refuses_after_reapproval_route(tmp_path):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_after_commit(db, dest)
    recover(db, 'PKG-1', 'require_reapproval')
    with pytest.raises(ValueError, match='delivery_reapproval_required'):
        release(db, 'PKG-1', VALUES, dest)


def test_prepare_is_refused_while_a_release_is_unresolved(tmp_path):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_after_commit(db, dest)
    with pytest.raises(ValueError, match='delivery_recovery_required'):
        prepare(db, 'PKG-1', 'req-1', VALUES)
    assert inspect(db, 'PKG-1')['release_status'] == 'committed'


def test_reapproval_can_prepare_a_new_attempt_without_rollback(tmp_path):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_after_commit(db, dest)
    recover(db, 'PKG-1', 'require_reapproval')
    history = recovery_rows(db)
    assert prepare(db, 'PKG-1', 'req-1', VALUES)['state'] == 'prepared'
    assert recovery_rows(db) == history          # recovery history is append-only
    assert inspect(db, 'PKG-1')['release']['status'] == 'reapproval_required'
    assert release(db, 'PKG-1', VALUES, dest)['release_status'] == 'delivered'
    assert dest.is_file()
    assert recovery_rows(db) == history
    # Both attempts stay in the ledger; only the newest one is current.
    con = connect(db)
    attempts = [r['attempt'] for r in con.execute(
        'SELECT attempt FROM delivery_release WHERE package_id=? ORDER BY attempt', ('PKG-1',))]
    con.close()
    assert attempts == [1, 2]


def test_prepare_window_retry_discards_only_a_writeless_intent(tmp_path):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_during_release(db, dest, 'prepare')
    assert recover(db, 'PKG-1', 'promote_verified')['reason'] == 'temp_missing'
    # No bytes reached disk, so a retry may start a new attempt.
    assert release(db, 'PKG-1', VALUES, dest)['release_status'] == 'delivered'
    assert dest.is_file()
    assert recovery_rows(db)[-1]['outcome'] == 'evidence_incomplete'
    con = connect(db)
    attempts = [r['attempt'] for r in con.execute(
        'SELECT attempt FROM delivery_release WHERE package_id=? ORDER BY attempt', ('PKG-1',))]
    con.close()
    assert attempts == [1, 2]


def test_partially_written_pending_is_not_discarded_for_retry(tmp_path):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_during_release(db, dest, 'temp')
    # Even an empty pending file is evidence: release must not replace it.
    pending(dest).write_bytes(b'')
    with pytest.raises(ValueError, match='delivery_recovery_required'):
        release(db, 'PKG-1', VALUES, dest)
    assert not dest.exists()


# ---------------------------------------------------------------------- usage audit

def test_audit_requires_destination_and_proof_after_interrupted_release(tmp_path):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_after_commit(db, dest)
    record(db, [USE])
    # Ledger says released, but the destination does not exist: not compliant.
    result = audit(db, 'PKG-1')
    assert result['status'] == 'uncertain'
    assert result['events'][0]['reason'].startswith('delivery_evidence_missing')
    # Promotion makes the same use auditable against its authorization time.
    recover(db, 'PKG-1', 'promote_verified')
    assert audit(db, 'PKG-1')['status'] == 'compliant'


@pytest.mark.parametrize('window', ('temp', 'commit'))
def test_reapproval_route_keeps_usage_uncertain(tmp_path, window):
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_during_release(db, dest, window)
    record(db, [USE])
    withheld = recover(db, 'PKG-1', 'require_reapproval')
    assert withheld['delivered'] is False
    result = audit(db, 'PKG-1')
    assert result['status'] == 'uncertain'
    assert result['events'][0]['status'] == 'uncertain'


def test_rewritten_released_file_fails_audit_evidence(tmp_path):
    db = ready(tmp_path)
    dest = tmp_path / 'out.json'
    release(db, 'PKG-1', VALUES, dest)
    record(db, [USE])
    assert audit(db, 'PKG-1')['status'] == 'compliant'
    payload = json.loads(dest.read_text(encoding='utf-8'))
    payload['fields']['score_band'] = 'B'
    dest.write_text(json.dumps(payload), encoding='utf-8')
    assert audit(db, 'PKG-1')['status'] == 'uncertain'
    # evidence is recomputed, never repaired by a state string
    assert inspect(db, 'PKG-1')['state'] == 'released'
    assert inspect(db, 'PKG-1')['delivery_evidence']['ok'] is False


def test_state_string_alone_never_unlocks_authorization_audit(tmp_path):
    db = ready(tmp_path)
    record(db, [USE])
    # Simulate a half-written ledger claim with no release evidence at all.
    poke_ledger(db, "UPDATE delivery_package SET state='released',released_at=CURRENT_TIMESTAMP,"
                    "release_status='delivered',output_path=? WHERE id=?",
                (str(tmp_path / 'nowhere.json'), 'PKG-1'))
    result = audit(db, 'PKG-1')
    assert result['status'] == 'uncertain'
    assert result['events'][0]['reason'] == 'delivery_evidence_missing:release_evidence_absent'


def test_prepare_state_cannot_be_audited_as_delivered(tmp_path):
    db = ready(tmp_path)
    record(db, [USE])
    assert audit(db, 'PKG-1')['status'] == 'uncertain'
    assert inspect(db, 'PKG-1')['state'] == 'prepared'


# --------------------------------------------------------- history / old packages

def test_old_successful_package_stays_readable_when_a_new_one_recovers(tmp_path):
    db = ready(tmp_path)
    old_dest = tmp_path / 'old.json'
    release(db, 'PKG-1', VALUES, old_dest)
    record(db, [USE])
    assert audit(db, 'PKG-1')['status'] == 'compliant'
    old_bytes = old_dest.read_bytes()

    prepare(db, 'PKG-2', 'req-1', VALUES)
    new_dest = tmp_path / 'new.json'
    crash_after_commit(db, new_dest, package_id='PKG-2')
    # Recover PKG-2 while PKG-1 must remain untouched and auditable.
    assert recover(db, 'PKG-2', 'promote_verified')['outcome'] == 'delivered'
    assert old_dest.read_bytes() == old_bytes
    assert inspect(db, 'PKG-1')['delivery_evidence']['ok'] is True
    assert audit(db, 'PKG-1')['status'] == 'compliant'
    assert inspect(db, 'PKG-2')['release_status'] == 'delivered'


def test_legacy_released_row_is_migrated_only_when_file_recomputes(tmp_path):
    db = ready(tmp_path)
    dest = tmp_path / 'legacy.json'
    release(db, 'PKG-1', VALUES, dest)
    # Roll the ledger back to the pre-protocol shape: no proof row, no columns.
    con = connect(db)
    with con:
        con.execute('DROP TABLE delivery_release')
        con.execute('DROP TABLE delivery_recovery')
        con.execute('ALTER TABLE delivery_package DROP COLUMN release_status')
        con.execute('ALTER TABLE delivery_package DROP COLUMN output_digest')
    con.close()
    probe = connect(db)
    assert probe.execute("SELECT count(*) FROM sqlite_master WHERE type='table' "
                         "AND name='delivery_release'").fetchone()[0] == 0
    probe.close()
    row = inspect(db, 'PKG-1')           # ensure_schema migrates on first use
    assert row['release_status'] == 'delivered'
    assert row['release']['destination'] == str(dest)
    assert row['delivery_evidence']['ok'] is True


def test_legacy_released_row_with_missing_file_is_not_trusted(tmp_path):
    db = ready(tmp_path)
    dest = tmp_path / 'legacy.json'
    release(db, 'PKG-1', VALUES, dest)
    dest.unlink()
    con = connect(db)
    with con:
        con.execute('DROP TABLE delivery_release')
        con.execute('DROP TABLE delivery_recovery')
        con.execute('ALTER TABLE delivery_package DROP COLUMN release_status')
        con.execute('ALTER TABLE delivery_package DROP COLUMN output_digest')
    con.close()
    row = inspect(db, 'PKG-1')
    assert row['state'] == 'released'
    assert row['release'] is None
    assert row['delivery_evidence'] == {'ok': False, 'reason': 'release_evidence_absent'}
    record(db, [USE])
    assert audit(db, 'PKG-1')['status'] == 'uncertain'


def test_add_columns_migration_preserves_existing_rows(tmp_path):
    from purposegate.store import add_columns, columns, connect
    db = tmp_path / 'm.db'
    con = connect(db)
    with con:
        con.execute('CREATE TABLE t(id INTEGER PRIMARY KEY, keep TEXT NOT NULL)')
        con.execute("INSERT INTO t VALUES (1,'x')")
        add_columns(con, 't', (('release_status', 'TEXT'), ('output_digest', 'TEXT')))
        add_columns(con, 't', (('release_status', 'TEXT'),))    # idempotent
    assert columns(con, 't') == {'id', 'keep', 'release_status', 'output_digest'}
    row = con.execute('SELECT * FROM t').fetchone()
    assert row['keep'] == 'x' and row['release_status'] is None
    con.close()


# ---------------------------------------------------------------------------- CLI

def test_delivery_recover_cli(tmp_path):
    import contextlib
    import io
    import sys
    from purposegate.__main__ import main
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    crash_after_commit(db, dest)
    old = sys.argv
    try:
        sys.argv = ['purposegate', 'delivery-recover', '--db', str(db), '--id', 'PKG-1',
                    '--route', 'promote_verified']
        with contextlib.redirect_stdout(io.StringIO()) as out:
            main()
    finally:
        sys.argv = old
    result = json.loads(out.getvalue().strip())
    assert result['outcome'] == 'delivered'
    assert dest.is_file()


def test_delivery_recover_cli_fault_at(tmp_path):
    import contextlib
    import io
    import sys
    from purposegate.__main__ import main
    db = ready(tmp_path)
    dest = destination_free(tmp_path)
    values_file = tmp_path / 'values.json'
    values_file.write_text(json.dumps(VALUES), encoding='utf-8')
    old = sys.argv
    try:
        sys.argv = ['purposegate', 'delivery-release', '--db', str(db), '--id', 'PKG-1',
                    '--values', str(values_file), '--out', str(dest),
                    '--fault-at', 'commit']
        with pytest.raises(InjectedCrash):
            with contextlib.redirect_stdout(io.StringIO()):
                main()
    finally:
        sys.argv = old
    assert not dest.exists()
    assert pending(dest).is_file()
    assert inspect(db, 'PKG-1')['state'] == 'released'
    assert inspect(db, 'PKG-1')['release_status'] == 'committed'
