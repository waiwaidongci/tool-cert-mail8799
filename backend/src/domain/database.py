"""SQLite 持久化与连接管理。

设计要点：
- WAL 模式 + busy_timeout，允许 Web 线程并发读写。
- 每个工作线程独立连接（check_same_thread=False 下仍按线程取用）。
- 外键约束开启；金额/读数/修正量以 TEXT 存 Decimal 精确值。
"""

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    product TEXT NOT NULL,
    spec_lower TEXT NOT NULL,
    spec_upper TEXT NOT NULL,
    source_name TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pieces (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    piece_no TEXT NOT NULL,
    reading TEXT NOT NULL,
    UNIQUE(batch_id, piece_no),
    UNIQUE(batch_id, seq)
);

CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY,
    tool_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    range_lower TEXT NOT NULL,
    range_upper TEXT NOT NULL,
    source_name TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    superseded_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(tool_id, period_start, period_end, version)
);
CREATE INDEX IF NOT EXISTS idx_corr_lookup
    ON corrections(tool_id, period_start, period_end, version);

CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    piece_id INTEGER NOT NULL REFERENCES pieces(id) ON DELETE CASCADE,
    tool_id TEXT NOT NULL,
    measured_at TEXT NOT NULL,
    UNIQUE(piece_id)
);
CREATE INDEX IF NOT EXISTS idx_meas_batch_tool ON measurements(batch_id, tool_id, measured_at);

-- 一次复判尝试（请求）；更正会令受影响批次的当前结果失效并等待重算
CREATE TABLE IF NOT EXISTS rejudgment_runs (
    id INTEGER PRIMARY KEY,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,            -- running / current / superseded / pending
    reason TEXT NOT NULL DEFAULT 'manual',
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_batch ON rejudgment_runs(batch_id, id);

-- 每次运行产生的逐组结果版本
CREATE TABLE IF NOT EXISTS rejudgment_versions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES rejudgment_runs(id) ON DELETE CASCADE,
    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    tool_id TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT,
    group_piece_count INTEGER NOT NULL,
    range_lower TEXT,
    range_upper TEXT,
    min_count INTEGER,
    min_witness TEXT,
    max_count INTEGER,
    max_witness TEXT,
    status TEXT NOT NULL,            -- ok / pending
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ver_batch ON rejudgment_versions(batch_id, run_id);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT
);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, 'conn', None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA busy_timeout=30000')
            self._local.conn = conn
        return conn

    def initialize(self):
        with self._init_lock:
            conn = self.connect()
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(SCHEMA)
