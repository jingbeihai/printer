# -*- coding: utf-8 -*-
"""SQLite 持久化。云端与本地是两个独立进程,各自持有一份库。
本模块为进程内单例: init(path, schema) 后用 q/one/execute 访问。"""
import sqlite3
import threading
import time
from contextlib import contextmanager

_lock = threading.RLock()
_conn = None
_postgres = False

# ---------------- 云端 schema ----------------
SERVER_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'teacher',   -- admin | teacher
  profile_json TEXT NOT NULL DEFAULT '{}',-- 提交页表单记忆
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  school TEXT DEFAULT '', grade TEXT DEFAULT '',
  classes INTEGER DEFAULT 0,
  copies INTEGER NOT NULL,               -- 实际总份数，供打印执行器使用
  copies_per_class INTEGER,              -- 每班份数，供界面与统计展示
  pages INTEGER NOT NULL,
  sheets INTEGER NOT NULL,               -- 估算总张数(不含封面)
  note TEXT DEFAULT '',
  source_user TEXT NOT NULL,             -- 提交人账号
  priority INTEGER NOT NULL DEFAULT 0,   -- 管理员插队置顶用,越大越靠前
  status TEXT NOT NULL DEFAULT 'QUEUED', -- QUEUED/PRINTING/AWAITING_PICKUP/DONE/CANCELED
  pdf_path TEXT NOT NULL, pdf_name TEXT NOT NULL,
  created_at REAL NOT NULL, started_at REAL, done_at REAL
);
CREATE TABLE IF NOT EXISTS task_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, task_id INTEGER, type TEXT NOT NULL, detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS config_kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS school_presets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  school TEXT NOT NULL,
  grade TEXT NOT NULL,
  classes INTEGER NOT NULL DEFAULT 0 CHECK(classes >= 0),
  copies INTEGER NOT NULL DEFAULT 1 CHECK(copies >= 1),
  UNIQUE(school, grade)
);
"""

# ---------------- 本地代理 schema ----------------
AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS printers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  ip TEXT DEFAULT '',
  queue_name TEXT DEFAULT '',
  backend TEXT DEFAULT '',
  state TEXT NOT NULL DEFAULT 'OFFLINE',
  enabled INTEGER NOT NULL DEFAULT 1,
  current_group INTEGER,
  page_counter INTEGER NOT NULL DEFAULT 0,
  pages_total INTEGER NOT NULL DEFAULT 0,
  tray_sheets INTEGER NOT NULL DEFAULT 250,
  note TEXT DEFAULT '',
  last_seen REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS printer_run_sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  printer_id INTEGER NOT NULL, group_id INTEGER,
  started_at REAL NOT NULL, ended_at REAL,
  active_seconds REAL NOT NULL DEFAULT 0,
  start_counter INTEGER, end_counter INTEGER,
  end_reason TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS printer_rest_sessions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  printer_id INTEGER NOT NULL,
  started_at REAL NOT NULL, ended_at REAL,
  required_seconds INTEGER NOT NULL,
  override_user TEXT DEFAULT '', override_reason TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS local_tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cloud_id INTEGER UNIQUE NOT NULL,      -- 云端任务ID(对账键)
  title TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}',  -- school/grade/classes/note/source_user
  copies INTEGER NOT NULL, pages INTEGER NOT NULL,
  pdf_path TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'QUEUED',
  priority INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, done_at REAL
);
CREATE TABLE IF NOT EXISTS groups(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL,              -- local_tasks.id
  seq INTEGER NOT NULL,
  label TEXT NOT NULL,
  pdf_path TEXT NOT NULL,
  pages INTEGER NOT NULL, copies INTEGER NOT NULL, sheets INTEGER NOT NULL,
  composed_path TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'QUEUED',
  printer_id INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
  start_counter INTEGER,
  started_at REAL, done_at REAL
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  printer_id INTEGER, group_id INTEGER, task_id INTEGER,
  type TEXT NOT NULL, detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS config_kv(key TEXT PRIMARY KEY, value TEXT);
"""


def _pg_sql(sql: str) -> str:
    """当前业务 SQL 采用 qmark 参数；在 PostgreSQL 驱动中转换为 %s。"""
    return sql.replace("?", "%s")


def _pg_schema(schema: str) -> str:
    return (schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
                  .replace(" REAL", " DOUBLE PRECISION"))


def init(path, schema):
    global _conn, _postgres
    target = str(path)
    _postgres = target.startswith(("postgres://", "postgresql://"))
    if _postgres:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL 模式需要 psycopg，请安装 requirements.txt") from exc
        _conn = psycopg.connect(target, row_factory=dict_row)
    else:
        _conn = sqlite3.connect(target, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    with _lock:
        if _postgres:
            for statement in _pg_schema(schema).split(";"):
                if statement.strip():
                    _conn.execute(statement)
        else:
            _conn.executescript(schema)
        # 兼容早期 SQLite 数据库：新增列以无损方式补齐。
        if "printers" in schema and not _postgres:
            existing = {r["name"] for r in _conn.execute("PRAGMA table_info(printers)")}
            for name, definition in (
                ("continuous_print_seconds", "REAL NOT NULL DEFAULT 0"),
                ("max_continuous_minutes", "INTEGER NOT NULL DEFAULT 120"),
                ("min_rest_minutes", "INTEGER NOT NULL DEFAULT 30"),
                ("warn_before_minutes", "INTEGER NOT NULL DEFAULT 15"),
                ("rest_until", "REAL NOT NULL DEFAULT 0"),
            ):
                if name not in existing:
                    _conn.execute(f"ALTER TABLE printers ADD COLUMN {name} {definition}")
        if "tasks" in schema:
            if _postgres:
                _conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS copies_per_class INTEGER")
            else:
                existing = {r["name"] for r in _conn.execute("PRAGMA table_info(tasks)")}
                if "copies_per_class" not in existing:
                    _conn.execute("ALTER TABLE tasks ADD COLUMN copies_per_class INTEGER")
        # SQLite 专有维护语句；PostgreSQL 不支持 PRAGMA。
        if not _postgres:
            _conn.execute("PRAGMA optimize")
        _conn.commit()


@contextmanager
def db():
    with _lock:
        yield _conn
        _conn.commit()


def q(sql, args=()):
    with _lock:
        return _conn.execute(_pg_sql(sql) if _postgres else sql, args).fetchall()


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def execute(sql, args=()):
    with _lock:
        query = _pg_sql(sql) if _postgres else sql
        # psycopg 没有 SQLite 的 lastrowid。只有业务实体 INSERT 追加 RETURNING id。
        if _postgres and query.lstrip().upper().startswith("INSERT INTO "):
            entity = query.lstrip().split()[2].split("(")[0].lower()
            if entity in {"users", "tasks", "printers", "local_tasks", "groups",
                          "printer_run_sessions", "printer_rest_sessions"}:
                cur = _conn.execute(query + " RETURNING id", args)
                row = cur.fetchone()
                _conn.commit()
                return row["id"]
        cur = _conn.execute(query, args)
        _conn.commit()
        return getattr(cur, "lastrowid", None)


def log_event(type_, detail="", printer_id=None, group_id=None, task_id=None,
              table="events"):
    if table == "events":
        execute("INSERT INTO events(ts,printer_id,group_id,task_id,type,detail)"
                " VALUES(?,?,?,?,?,?)",
                (time.time(), printer_id, group_id, task_id, type_, detail))
    else:  # 云端 task_events
        execute("INSERT INTO task_events(ts,task_id,type,detail) VALUES(?,?,?,?)",
                (time.time(), task_id, type_, detail))


def get_conf(key, default=""):
    row = one("SELECT value FROM config_kv WHERE key=?", (key,))
    return row["value"] if row else default


def set_conf(key, value):
    execute("INSERT INTO config_kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
