"""SQLite 存储层。所有 JSON 字段以 TEXT 存储，读取时解析。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    current_version_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contract_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    version INTEGER NOT NULL,
    content_json TEXT NOT NULL,          -- {fields, partitions, quality_rules, dependencies}
    status TEXT NOT NULL DEFAULT 'published',
    created_by TEXT NOT NULL DEFAULT '',
    change_request_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(contract_id, version)
);

CREATE TABLE IF NOT EXISTS change_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    title TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    base_version_id INTEGER,
    proposed_json TEXT NOT NULL,
    diff_json TEXT NOT NULL DEFAULT '[]',
    gate_status TEXT NOT NULL DEFAULT 'PASS',   -- PASS | NEEDS_REVIEW | BLOCKED
    gate_detail_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',        -- open | merged | rejected
    auto_merged INTEGER NOT NULL DEFAULT 0,
    merged_version_id INTEGER,
    created_at TEXT NOT NULL,
    merged_at TEXT
);

CREATE TABLE IF NOT EXISTS impact_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_request_id INTEGER NOT NULL REFERENCES change_requests(id),
    report_json TEXT NOT NULL,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_request_id INTEGER NOT NULL REFERENCES change_requests(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    assignee_team TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'todo',        -- todo | in_progress | done
    source_change_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_request_id INTEGER NOT NULL REFERENCES change_requests(id),
    approver TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'owner',         -- owner | steward | downstream_owner
    decision TEXT NOT NULL,                     -- approve | reject
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rulesets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    rules_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',      -- active | superseded
    parent_id INTEGER,
    rollback_of_id INTEGER,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(name, version)
);
"""


def db_path() -> str:
    return os.environ.get(
        "CONTRACT_CI_DB",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "contract_ci.db"),
    )


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def query(sql: str, args: tuple = ()) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def query_one(sql: str, args: tuple = ()) -> dict | None:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: tuple = ()) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid or 0
    finally:
        conn.close()


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def loads(text: str, default=None):
    if text is None:
        return default
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return default
