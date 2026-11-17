import sqlite3
from pathlib import Path
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS batch(id TEXT PRIMARY KEY, config_hash TEXT NOT NULL, config_raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS source_file(id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batch(id), name TEXT NOT NULL, digest TEXT NOT NULL, raw BLOB NOT NULL, UNIQUE(batch_id,digest));
CREATE TABLE IF NOT EXISTS raw_event(id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES source_file(id), line_no INTEGER NOT NULL, event_key TEXT NOT NULL, subject TEXT NOT NULL, kind TEXT NOT NULL, at TEXT, payload TEXT NOT NULL, digest TEXT NOT NULL, UNIQUE(source_id,line_no));
CREATE TABLE IF NOT EXISTS subject(id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batch(id));
CREATE TABLE IF NOT EXISTS rule_version(id TEXT PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batch(id), status TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, ended_at TEXT);
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES run(id), source_id INTEGER REFERENCES source_file(id), line_no INTEGER, subject TEXT, code TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES run(id), subject TEXT NOT NULL, source_id INTEGER REFERENCES source_file(id), event_id INTEGER REFERENCES raw_event(id), rule_id TEXT REFERENCES rule_version(id), detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES run(id), subject TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, UNIQUE(run_id,subject));
CREATE TABLE IF NOT EXISTS execution(id INTEGER PRIMARY KEY, run_id INTEGER REFERENCES run(id), subject TEXT NOT NULL, action TEXT NOT NULL, before_hash TEXT, after_state TEXT NOT NULL, at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(subject,action,before_hash));
CREATE TABLE IF NOT EXISTS plan(id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL REFERENCES run(id), digest TEXT NOT NULL UNIQUE, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dp_commit(batch_no INTEGER PRIMARY KEY, contract TEXT NOT NULL CHECK(contract IN ('sequential','parallel_disjoint')), decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dp_release(release_id TEXT PRIMARY KEY, batch_no INTEGER NOT NULL REFERENCES dp_commit(batch_no), content_hash TEXT NOT NULL, subject TEXT NOT NULL, purpose TEXT NOT NULL, dataset_version TEXT NOT NULL, fields_json TEXT NOT NULL, persons_json TEXT NOT NULL, epsilon REAL NOT NULL CHECK(epsilon>=0), delta REAL NOT NULL CHECK(delta>=0), mechanism_json TEXT NOT NULL, request_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dp_release_source(release_id TEXT NOT NULL REFERENCES dp_release(release_id), event_key TEXT NOT NULL, event_id INTEGER NOT NULL REFERENCES raw_event(id), person TEXT NOT NULL, pool_purpose TEXT NOT NULL, PRIMARY KEY(release_id,event_key,person));
CREATE TABLE IF NOT EXISTS dp_pool_ledger(release_id TEXT NOT NULL REFERENCES dp_release(release_id), pool_subject TEXT NOT NULL, pool_purpose TEXT NOT NULL, pool_version TEXT NOT NULL, epsilon REAL NOT NULL, delta REAL NOT NULL, PRIMARY KEY(release_id,pool_subject,pool_purpose,pool_version));
"""
def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
