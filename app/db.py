"""SQLite 持久层：阀门档案、测试（含采样）、分析版本、配对结果。"""

import json
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS valves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tag TEXT UNIQUE NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  valve_id INTEGER NOT NULL REFERENCES valves(id),
  phase TEXT NOT NULL,
  submitted_at TEXT NOT NULL,
  test_started_at TEXT,
  range_min REAL NOT NULL,
  range_max REAL NOT NULL,
  range_unit TEXT NOT NULL,
  conditions_json TEXT NOT NULL,
  thresholds_json TEXT NOT NULL,
  calibration_valid_until TEXT,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  test_id INTEGER NOT NULL REFERENCES tests(id),
  version INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT 'auto',
  adjustments_json TEXT NOT NULL DEFAULT '[]',
  result_json TEXT NOT NULL,
  UNIQUE(test_id, version)
);
CREATE TABLE IF NOT EXISTS pairings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pre_analysis_id INTEGER NOT NULL,
  post_analysis_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  result_json TEXT NOT NULL
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    def close(self):
        self._conn.close()

    # ---- 阀门档案 ----
    def create_valve(self, tag, description=""):
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO valves(tag, description, created_at) VALUES(?,?,?)",
                (tag, description, _now()))
            return cur.lastrowid

    def get_valve(self, valve_id):
        row = self._conn.execute("SELECT * FROM valves WHERE id=?", (valve_id,)).fetchone()
        return dict(row) if row else None

    def get_valve_by_tag(self, tag):
        row = self._conn.execute("SELECT * FROM valves WHERE tag=?", (tag,)).fetchone()
        return dict(row) if row else None

    def list_valves(self):
        return [dict(r) for r in self._conn.execute("SELECT * FROM valves ORDER BY id")]

    # ---- 测试 ----
    def create_test(self, valve_id, phase, test_started_at, range_min, range_max,
                    range_unit, conditions, thresholds, calibration_valid_until, payload):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO tests(valve_id, phase, submitted_at, test_started_at,
                   range_min, range_max, range_unit, conditions_json, thresholds_json,
                   calibration_valid_until, payload_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (valve_id, phase, _now(), test_started_at, range_min, range_max,
                 range_unit, json.dumps(conditions), json.dumps(thresholds),
                 calibration_valid_until, json.dumps(payload)))
            return cur.lastrowid

    def get_test(self, test_id):
        row = self._conn.execute("SELECT * FROM tests WHERE id=?", (test_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["conditions"] = json.loads(d.pop("conditions_json"))
        d["thresholds"] = json.loads(d.pop("thresholds_json"))
        d["payload"] = json.loads(d.pop("payload_json"))
        return d

    def list_tests(self, valve_id=None):
        q = "SELECT id, valve_id, phase, submitted_at, test_started_at FROM tests"
        args = ()
        if valve_id is not None:
            q += " WHERE valve_id=?"
            args = (valve_id,)
        return [dict(r) for r in self._conn.execute(q + " ORDER BY id", args)]

    # ---- 分析版本 ----
    def create_analysis(self, test_id, author, adjustments, result):
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM analyses WHERE test_id=?",
                (test_id,)).fetchone()
            version = row["v"] + 1
            cur = self._conn.execute(
                """INSERT INTO analyses(test_id, version, created_at, author,
                   adjustments_json, result_json) VALUES(?,?,?,?,?,?)""",
                (test_id, version, _now(), author,
                 json.dumps(adjustments), json.dumps(result)))
            return cur.lastrowid, version

    def get_analysis(self, analysis_id):
        row = self._conn.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
        return self._analysis_row(row)

    def get_analysis_by_version(self, test_id, version):
        row = self._conn.execute(
            "SELECT * FROM analyses WHERE test_id=? AND version=?",
            (test_id, version)).fetchone()
        return self._analysis_row(row)

    def list_analyses(self, test_id):
        rows = self._conn.execute(
            """SELECT id, test_id, version, created_at, author FROM analyses
               WHERE test_id=? ORDER BY version""", (test_id,)).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _analysis_row(row):
        if not row:
            return None
        d = dict(row)
        d["adjustments"] = json.loads(d.pop("adjustments_json"))
        d["result"] = json.loads(d.pop("result_json"))
        return d

    # ---- 配对 ----
    def create_pairing(self, pre_analysis_id, post_analysis_id, result):
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO pairings(pre_analysis_id, post_analysis_id, created_at, result_json)
                   VALUES(?,?,?,?)""",
                (pre_analysis_id, post_analysis_id, _now(), json.dumps(result)))
            return cur.lastrowid

    def get_pairing(self, pairing_id):
        row = self._conn.execute("SELECT * FROM pairings WHERE id=?", (pairing_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["result"] = json.loads(d.pop("result_json"))
        return d
