"""The runner's state in SQLite: jobs, items, uploads, speed samples, events and a few settings.

WAL journal, full sync and short transactions, so a power cut leaves the database consistent and at most loses the
last write. Every thread gets its own connection.
"""

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    device TEXT NOT NULL DEFAULT 'cpu',
    executor TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 100,
    position INTEGER NOT NULL DEFAULT 0,
    definition TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'waiting',
    reason TEXT NOT NULL DEFAULT '',
    load_error TEXT NOT NULL DEFAULT '',
    inputs_ok INTEGER NOT NULL DEFAULT 0,
    inputs_reason TEXT NOT NULL DEFAULT '',
    models_ok INTEGER NOT NULL DEFAULT 0,
    models_reason TEXT NOT NULL DEFAULT '',
    items_known INTEGER NOT NULL DEFAULT 1,
    removed INTEGER NOT NULL DEFAULT 0,
    repeat TEXT NOT NULL DEFAULT '',
    last_run_at REAL,
    last_result TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS items (
    asset_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    slug TEXT NOT NULL,
    data TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    active_phase TEXT NOT NULL DEFAULT '',
    frames_done INTEGER NOT NULL DEFAULT 0,
    frames_total INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    oom_retries INTEGER NOT NULL DEFAULT 0,
    failure_kind TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    registry_checked INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    pid INTEGER,
    pid_started REAL,
    started_at REAL,
    rendered_at REAL,
    done_at REAL,
    render_seconds REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_job_idx ON items (job_id, position);
CREATE INDEX IF NOT EXISTS items_state_idx ON items (state);
CREATE TABLE IF NOT EXISTS uploads (
    asset_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'waiting',
    bytes INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    first_failure_at REAL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    confirmed_at REAL,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS speed_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    unit TEXT NOT NULL,
    seconds REAL NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS speed_job_idx ON speed_samples (job_id, id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    job_id TEXT,
    asset_id TEXT
);
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    verified_at REAL,
    error TEXT NOT NULL DEFAULT ''
);
"""

SPEED_WINDOW = 20
# Columns added after the first release: (table, column, definition). A node that updates itself keeps its database,
# so new columns are added in place.
ADDED_COLUMNS = (
    ('jobs', 'repeat', "TEXT NOT NULL DEFAULT ''"),
    ('jobs', 'last_run_at', 'REAL'),
    ('jobs', 'last_result', "TEXT NOT NULL DEFAULT ''"),
)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()

    def connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, 'connection', None)
        if connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA busy_timeout=30000')
            self._local.connection = connection
        return connection

    def close(self) -> None:
        connection = getattr(self._local, 'connection', None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    def init_schema(self) -> None:
        connection = self.connection()
        connection.executescript(SCHEMA)
        for table, column, definition in ADDED_COLUMNS:
            existing = {row['name'] for row in connection.execute(f'PRAGMA table_info({table})')}
            if column not in existing:
                connection.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connection()
        connection.execute('BEGIN IMMEDIATE')
        try:
            yield connection
        except BaseException:
            connection.execute('ROLLBACK')
            raise
        connection.execute('COMMIT')

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        with self.transaction() as connection:
            connection.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return list(self.connection().execute(sql, params).fetchall())

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.connection().execute(sql, params).fetchone()

    # meta

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.one('SELECT value FROM meta WHERE key = ?', (key,))
        return default if row is None else row['value']

    def set_meta(self, key: str, value: object) -> None:
        text = value if isinstance(value, str) else json.dumps(value)
        self.execute('INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                     (key, text))

    def delete_meta(self, key: str) -> None:
        self.execute('DELETE FROM meta WHERE key = ?', (key,))

    # events

    def add_event(self, level: str, message: str, job_id: str | None = None, asset_id: str | None = None) -> None:
        self.execute('INSERT INTO events (at, level, message, job_id, asset_id) VALUES (?, ?, ?, ?, ?)',
                     (time.time(), level, message[:2000], job_id, asset_id))

    def last_error(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM events WHERE level = 'error' ORDER BY id DESC LIMIT 1")

    # speeds

    def add_speed_sample(self, job_id: str, unit: str, seconds: float) -> None:
        with self.transaction() as connection:
            connection.execute('INSERT INTO speed_samples (job_id, unit, seconds, at) VALUES (?, ?, ?, ?)',
                               (job_id, unit, seconds, time.time()))
            # Keep the table small: only the latest window per job matters.
            connection.execute(
                'DELETE FROM speed_samples WHERE job_id = ? AND id NOT IN '
                '(SELECT id FROM speed_samples WHERE job_id = ? ORDER BY id DESC LIMIT ?)',
                (job_id, job_id, SPEED_WINDOW * 5),
            )

    def average_speed(self, job_id: str, unit: str) -> float | None:
        rows = self.query('SELECT seconds FROM speed_samples WHERE job_id = ? AND unit = ? ORDER BY id DESC LIMIT ?',
                          (job_id, unit, SPEED_WINDOW))
        if not rows:
            return None
        return sum(row['seconds'] for row in rows) / len(rows)

    # items

    def update_item(self, asset_id: str, **fields: object) -> None:
        if not fields:
            return
        fields['updated_at'] = time.time()
        assignments = ', '.join(f'{name} = ?' for name in fields)
        self.execute(f'UPDATE items SET {assignments} WHERE asset_id = ?', (*fields.values(), asset_id))

    def item(self, asset_id: str) -> sqlite3.Row | None:
        return self.one('SELECT * FROM items WHERE asset_id = ?', (asset_id,))

    def job(self, job_id: str) -> sqlite3.Row | None:
        return self.one('SELECT * FROM jobs WHERE id = ?', (job_id,))

    def update_job(self, job_id: str, **fields: object) -> None:
        if not fields:
            return
        fields['updated_at'] = time.time()
        assignments = ', '.join(f'{name} = ?' for name in fields)
        self.execute(f'UPDATE jobs SET {assignments} WHERE id = ?', (*fields.values(), job_id))

    def upsert_upload(self, asset_id: str, job_id: str, **fields: object) -> None:
        fields['updated_at'] = time.time()
        names = ['asset_id', 'job_id', *fields.keys()]
        placeholders = ', '.join('?' for _ in names)
        updates = ', '.join(f'{name} = excluded.{name}' for name in ['job_id', *fields.keys()])
        self.execute(
            f'INSERT INTO uploads ({", ".join(names)}) VALUES ({placeholders}) '
            f'ON CONFLICT(asset_id) DO UPDATE SET {updates}',
            (asset_id, job_id, *fields.values()),
        )
